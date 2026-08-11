from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from control.models import AuditEvent, Job, LedgerEntry, Payout, TreasuryBalance
from control.payout_state import assert_payout_transition
from control.services.jobs import transition_job

ZERO = Decimal("0")


def _post_once(*, entry_key: str, reference: str, account: str, counter_account: str, amount: Decimal, currency: str, event_type: str, metadata: dict):
    if amount < ZERO:
        raise ValueError("ledger amount must be non-negative")
    existing = LedgerEntry.objects.filter(entry_key=entry_key).first()
    if existing:
        expected = (reference, account, counter_account, amount, currency, event_type)
        actual = (existing.reference, existing.account, existing.counter_account, existing.amount, existing.currency, existing.event_type)
        if actual != expected:
            raise ValueError(f"ledger idempotency conflict for {entry_key}")
        return existing
    return LedgerEntry.objects.create(
        entry_key=entry_key,
        reference=reference,
        account=account,
        counter_account=counter_account,
        amount=amount,
        currency=currency,
        event_type=event_type,
        metadata=metadata,
    )


def _recompute_treasury(job: Job, currency: str) -> TreasuryBalance:
    payouts = Payout.objects.filter(job__marketplace=job.marketplace, currency=currency)
    earned = payouts.filter(state__in=[Payout.State.EARNED, Payout.State.PAYOUT_PENDING, Payout.State.SETTLED]).aggregate(total=Sum("net"))["total"] or ZERO
    pending = payouts.filter(state=Payout.State.PAYOUT_PENDING).aggregate(total=Sum("net"))["total"] or ZERO
    settled = payouts.filter(state=Payout.State.SETTLED).aggregate(total=Sum("net"))["total"] or ZERO
    treasury, _ = TreasuryBalance.objects.update_or_create(
        account=job.marketplace.slug,
        currency=currency,
        defaults={"marketplace": job.marketplace, "earned": earned, "pending": pending, "settled": settled},
    )
    return treasury


def _validate_job_money_state(job: Job, current_payout_state: str | None, target_state: str, *, advance_job_state: bool) -> None:
    if not advance_job_state:
        source_type = str((job.normalized_payload or {}).get("source_type") or "")
        if source_type != "INBOUND_SERVICE_ORDER" or job.state == Job.State.FAILED:
            raise ValueError("non-advancing payout lifecycle is restricted to funded direct-commerce orders")
        return
    if current_payout_state is None and target_state == Payout.State.EARNED and job.state not in {Job.State.SUBMITTED, Job.State.ACCEPTED}:
        raise ValueError(f"job must be SUBMITTED or ACCEPTED before earnings can be recorded, got {job.state}")
    if target_state == Payout.State.PAYOUT_PENDING and job.state not in {Job.State.ACCEPTED, Job.State.PAYOUT_PENDING}:
        raise ValueError(f"job must be ACCEPTED before payout can be pending, got {job.state}")
    if target_state == Payout.State.SETTLED and job.state not in {Job.State.ACCEPTED, Job.State.PAYOUT_PENDING, Job.State.SETTLED}:
        raise ValueError(f"job must be accepted/pending before settlement, got {job.state}")
    if target_state == Payout.State.REVERSED and current_payout_state is None:
        raise ValueError("cannot reverse a payout that does not exist")


@transaction.atomic
def record_payout_state(
    *,
    job_id,
    target_state: str,
    gross: Decimal,
    fee: Decimal = ZERO,
    currency: str = "USD",
    external_reference: str = "",
    expected_date=None,
    settled_at=None,
    advance_job_state: bool = True,
) -> Payout:
    job = Job.objects.select_for_update().select_related("marketplace").get(pk=job_id)
    gross = Decimal(gross)
    fee = Decimal(fee)
    if gross < ZERO or fee < ZERO or fee > gross:
        raise ValueError("invalid payout gross/fee")
    currency = currency.upper()[:3]
    payout = Payout.objects.select_for_update().filter(job=job, currency=currency).first()
    current = payout.state if payout else None
    assert_payout_transition(current, target_state)
    _validate_job_money_state(job, current, target_state, advance_job_state=advance_job_state)
    net = gross - fee

    if payout is not None and (payout.gross != gross or payout.fee != fee or payout.net != net):
        raise ValueError("payout amount mutation requires an explicit adjustment workflow")

    if payout is None:
        payout = Payout.objects.create(
            job=job,
            gross=gross,
            fee=fee,
            net=net,
            currency=currency,
            external_reference=external_reference,
            state=target_state,
            expected_date=expected_date,
            earned_at=timezone.now() if target_state == Payout.State.EARNED else None,
            pending_at=timezone.now() if target_state == Payout.State.PAYOUT_PENDING else None,
            settled_at=settled_at if target_state == Payout.State.SETTLED else None,
        )
    else:
        payout.external_reference = external_reference or payout.external_reference
        payout.state = target_state
        payout.expected_date = expected_date or payout.expected_date
        if target_state == Payout.State.EARNED and payout.earned_at is None:
            payout.earned_at = timezone.now()
        if target_state == Payout.State.PAYOUT_PENDING and payout.pending_at is None:
            payout.pending_at = timezone.now()
        if target_state == Payout.State.SETTLED:
            payout.pending_at = payout.pending_at or timezone.now()
            payout.settled_at = settled_at or payout.settled_at or timezone.now()
        payout.save(update_fields=["external_reference", "state", "expected_date", "earned_at", "pending_at", "settled_at", "updated_at"])

    reference = external_reference or payout.external_reference or f"job:{job.id}"
    if target_state == Payout.State.EARNED:
        if advance_job_state and job.state == Job.State.SUBMITTED:
            transition_job(job.id, Job.State.ACCEPTED, actor="treasury", metadata={"payout_id": payout.id})
        _post_once(
            entry_key=f"payout:{payout.id}:earned",
            reference=reference,
            account=f"receivable:{job.marketplace.slug}",
            counter_account="earned_revenue",
            amount=net,
            currency=currency,
            event_type="PAYOUT_EARNED",
            metadata={"job_id": str(job.id), "payout_id": payout.id},
        )
    elif target_state == Payout.State.PAYOUT_PENDING:
        if advance_job_state and job.state == Job.State.ACCEPTED:
            transition_job(job.id, Job.State.PAYOUT_PENDING, actor="treasury", metadata={"payout_id": payout.id})
    elif target_state == Payout.State.SETTLED:
        if advance_job_state and job.state == Job.State.ACCEPTED:
            transition_job(job.id, Job.State.PAYOUT_PENDING, actor="treasury", metadata={"payout_id": payout.id})
            job.refresh_from_db()
        if advance_job_state and job.state == Job.State.PAYOUT_PENDING:
            transition_job(job.id, Job.State.SETTLED, actor="treasury", metadata={"payout_id": payout.id})
        _post_once(
            entry_key=f"payout:{payout.id}:settled",
            reference=reference,
            account=f"cash:{job.marketplace.slug}",
            counter_account=f"receivable:{job.marketplace.slug}",
            amount=net,
            currency=currency,
            event_type="PAYOUT_SETTLED",
            metadata={"job_id": str(job.id), "payout_id": payout.id},
        )
    elif target_state == Payout.State.REVERSED:
        # Reversal is append-only economic history. Prior earned/settled entries remain auditable.
        _post_once(
            entry_key=f"payout:{payout.id}:reversed",
            reference=reference,
            account="reversal_loss",
            counter_account=f"receivable:{job.marketplace.slug}",
            amount=net,
            currency=currency,
            event_type="PAYOUT_REVERSED",
            metadata={"job_id": str(job.id), "payout_id": payout.id},
        )

    treasury = _recompute_treasury(job, currency)
    if target_state in {Payout.State.SETTLED, Payout.State.REVERSED}:
        from control.services.genx_economics import record_settlement_outcome
        from control.services.product_factory import record_owned_product_payout

        record_settlement_outcome(payout=payout)
        record_owned_product_payout(payout=payout)
    AuditEvent.objects.create(
        event_type="payout.state_changed",
        actor="treasury",
        metadata={
            "job_id": str(job.id),
            "payout_id": payout.id,
            "from": current,
            "to": target_state,
            "net": str(net),
            "currency": currency,
            "treasury_settled": str(treasury.settled),
        },
    )
    return payout
