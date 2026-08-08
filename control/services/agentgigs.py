from __future__ import annotations

import hashlib
import os
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from control.agentgigs_lifecycle import authoritative_payout_from_earnings, details_decision, webhook_decision
from control.economics import EconomicsInput
from control.models import (
    Alert,
    Application,
    AuditEvent,
    Job,
    JobMessage,
    Marketplace,
    MarketplaceCredential,
    MarketHealth,
    Payout,
    PayoutAccount,
    Revision,
    Submission,
    WebhookEvent,
)
from control.secrets import decrypt_secret
from control.services.acquisition_runtime import AcquisitionError, acquire_profitable_job
from control.services.acquisition_preflight import run_acquisition_preflight
from control.services.autonomy import acquisition_autonomy
from control.services.finance import record_payout_state
from control.services.jobs import acquisition_decision, ingest_opportunity, score_and_persist, transition_job
from control.services.profit_brain import (
    UtilizationState,
    capture_capacity,
    current_growth_stage,
    discovery_limit,
    persist_pricing_strategy,
    recommend_price,
    record_reputation_snapshot,
    refresh_profit_intelligence,
)
from markets.agentgigs.client import AgentGigsAdapter, AgentGigsError
from markets.agentgigs.webhooks import AgentGigsWebhook


CENT = Decimal("0.01")
MAX_WEBHOOK_ATTEMPTS = 5
WEBHOOK_PROCESSING_STALE_SECONDS = 300


def _decimal_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except Exception:
        return Decimal(default)


def ensure_marketplace() -> Marketplace:
    market, _ = Marketplace.objects.get_or_create(
        slug="agentgigs",
        defaults={
            "display_name": "AgentGigs",
            "status": Marketplace.Status.PAYOUT_BLOCKED,
            "enabled": False,
            "payout_ready": False,
            "south_africa_verified": False,
            "payment_model": "Stripe Connect",
        },
    )
    return market


def _stored_api_key() -> str:
    market = ensure_marketplace()
    credential = MarketplaceCredential.objects.filter(
        marketplace=market,
        credential_type="api_key",
        active=True,
    ).order_by("-updated_at").first()
    return decrypt_secret(credential.encrypted_value) if credential else ""


def configured_adapter() -> AgentGigsAdapter:
    # Environment secrets are preferred for initial deployment. The encrypted
    # database credential is supported for later rotation through the control plane.
    api_key = os.getenv("AGENTGIGS_API_KEY", "") or _stored_api_key()
    return AgentGigsAdapter(
        api_key=api_key,
        base_url=os.getenv("AGENTGIGS_BASE_URL", "https://www.agentgigs.io"),
        timeout=int(os.getenv("AGENTGIGS_TIMEOUT_SECONDS", "20")),
    )


def _sync_verified_payout_gate(market: Marketplace) -> None:
    account = PayoutAccount.objects.filter(
        marketplace=market,
        south_africa_verified=True,
        status__in=["ACTIVE", "VERIFIED", "READY"],
    ).order_by("-verified_at", "-updated_at").first()
    if not account:
        return
    changed = []
    if not market.payout_ready:
        market.payout_ready = True
        changed.append("payout_ready")
    if not market.south_africa_verified:
        market.south_africa_verified = True
        changed.append("south_africa_verified")
    if changed:
        # Enabling/LIVE promotion is deliberately NOT automatic. A verified
        # payout record only opens the payout gate; policy/operations remain manual.
        market.save(update_fields=[*changed, "updated_at"])


def sync_market(adapter: AgentGigsAdapter | None = None, discover_limit: int = 100) -> dict:
    adapter = adapter or configured_adapter()
    market = ensure_marketplace()
    _sync_verified_payout_gate(market)
    health = adapter.health()
    earnings = None
    fee_rate = None
    if health.get("ok"):
        try:
            earnings = adapter.earnings()
            fee_rate = earnings.get("commissionRate")
        except AgentGigsError:
            earnings = None
    MarketHealth.objects.update_or_create(
        marketplace=market,
        defaults={
            "api_ok": bool(health.get("ok")),
            "auth_ok": bool(health.get("ok")),
            "payout_ok": bool(market.payout_ready and market.south_africa_verified),
            "supply_ok": int(health.get("count") or 0) > 0,
            "last_error_code": "" if health.get("ok") else str(health.get("error") or "UNKNOWN")[:120],
            "checked_at": timezone.now(),
            "details": {"health": health, "earnings_accessible": earnings is not None},
        },
    )
    if fee_rate is not None:
        try:
            parsed = Decimal(str(fee_rate))
            if Decimal("0") <= parsed < Decimal("1"):
                market.fee_rate = parsed
                market.save(update_fields=["fee_rate", "updated_at"])
        except Exception:
            pass

    agent_id = os.getenv("AGENTGIGS_AGENT_ID", "").strip()
    if health.get("ok") and agent_id:
        try:
            payload = adapter.reputation(agent_id)
            reputation = payload.get("reputation") if isinstance(payload.get("reputation"), dict) else {}
            record_reputation_snapshot(
                marketplace=market,
                source="agentgigs_public_api",
                rating=reputation.get("rating"),
                rating_count=len(payload.get("recentReviews", [])) if isinstance(payload.get("recentReviews"), list) else 0,
                completed_jobs=int(reputation.get("completedJobs") or 0),
                details={
                    "completion_rate": reputation.get("completionRate"),
                    "trust_level": reputation.get("trustLevel"),
                    "agent_verified": bool((payload.get("agent") or {}).get("verified")) if isinstance(payload.get("agent"), dict) else False,
                },
            )
        except (AgentGigsError, TypeError, ValueError):
            pass

    discovered = 0
    if health.get("ok"):
        for raw in adapter.discover_jobs(limit=max(1, min(int(discover_limit), 100))):
            opportunity = adapter.normalize_job(raw)
            ingest_opportunity(market, opportunity)
            discovered += 1
    return {"health": health, "discovered": discovered, "fee_rate": str(market.fee_rate)}


def _budget_usd(raw: dict, key: str) -> Decimal | None:
    try:
        cents = Decimal(str(raw.get(key)))
    except Exception:
        return None
    if cents < 0:
        return None
    return (cents / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)


def recommended_offer(job: Job) -> Decimal:
    raw = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    minimum = _budget_usd(raw, "budget_min")
    maximum = _budget_usd(raw, "budget_max")
    if minimum is None and maximum is None:
        return job.reward.quantize(CENT, rounding=ROUND_HALF_UP)
    if minimum is None:
        minimum = maximum
    if maximum is None:
        maximum = minimum
    assert minimum is not None and maximum is not None
    if maximum < minimum:
        minimum, maximum = maximum, minimum
    position = _decimal_env("AGENTGIGS_PROPOSAL_BUDGET_POSITION", "0.50")
    position = min(Decimal("1"), max(Decimal("0"), position))
    return (minimum + ((maximum - minimum) * position)).quantize(CENT, rounding=ROUND_HALF_UP)


def _auto_apply_enabled() -> bool:
    return acquisition_autonomy(switch_enabled=os.getenv("AGENTGIGS_AUTO_APPLY_ENABLED", "0") == "1").may_acquire


def score_open_jobs(limit: int = 100) -> dict:
    """Score open AgentGigs jobs from explicit configurable priors.

    Priors are bootstrap assumptions, never historical claims. As accepted/settled
    outcomes accumulate the wider learning layer can replace them with real data.
    """
    market = ensure_marketplace()
    scored = apply_ready = 0
    jobs = Job.objects.filter(
        marketplace=market,
        state__in=[Job.State.DISCOVERED, Job.State.EXPECTED],
    ).order_by("-updated_at")[: max(1, min(int(limit), 500))]
    capacity = capture_capacity(persist=True)
    for job in jobs:
        raw = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
        operation = str(raw.get("operation") or "unclassified")
        capability = operation
        stage = current_growth_stage(market, capability)
        advertised = _budget_usd(raw, "budget_max") or job.reward
        competitive = _budget_usd(raw, "competitive_price") or recommended_offer(job)
        expected_genx = _decimal_env("AGENTGIGS_EXPECTED_GENX_COST_USD", "0.25")
        expected_external = _decimal_env("AGENTGIGS_EXPECTED_EXTERNAL_COST_USD", "0")
        expected_local = _decimal_env("EXPECTED_OPERATIONAL_COST_PER_JOB_USD", "0.10")
        attempts = Application.objects.filter(job__marketplace=market).count()
        wins = Application.objects.filter(job__marketplace=market, status__in=["AWARDED", "ACCEPTED"]).count()
        historical_win_rate = None if attempts < 5 else Decimal(wins) / Decimal(attempts)
        recommendation = recommend_price(
            total_expected_cost=expected_genx + expected_external + expected_local,
            advertised_budget=advertised,
            competitive_price=competitive,
            fee_rate=market.fee_rate,
            utilization_state=UtilizationState(capacity.utilization_state),
            growth_stage=stage,
            historical_win_rate=historical_win_rate,
        )
        offer = recommendation.offered_price
        pricing = persist_pricing_strategy(
            job,
            capability=capability,
            operation=operation,
            recommendation=recommendation,
            capacity=capacity,
            stage=stage,
            advertised_budget=advertised,
            competitive_price=competitive,
        )
        fee = (offer * market.fee_rate).quantize(CENT, rounding=ROUND_HALF_UP)
        economics = EconomicsInput(
            gross_reward=offer,
            marketplace_fee=fee,
            p_acquire=_decimal_env("AGENTGIGS_P_ACQUIRE_PRIOR", "0.15"),
            p_accept=_decimal_env("AGENTGIGS_P_ACCEPT_PRIOR", "0.85"),
            p_payment=_decimal_env("AGENTGIGS_P_PAYMENT_PRIOR", "0.98"),
            expected_genx_cost=expected_genx,
            expected_external_cost=expected_external,
            expected_compute_cost=expected_local,
            estimated_worker_minutes=_decimal_env("AGENTGIGS_ESTIMATED_MINUTES_PRIOR", "60"),
        )
        reasons = ["PRIOR_BASED_SCORE"]
        if not _auto_apply_enabled():
            reasons.append("AUTO_APPLY_DISABLED")
        score_and_persist(
            job,
            economics,
            decision="WATCH",
            reason_codes=reasons,
            max_genx_credits=_decimal_env("AGENTGIGS_MAX_GENX_CREDITS", "0"),
            recommended_offer=offer,
        )
        preflight = run_acquisition_preflight(job)
        latest_decision = job.opportunity_decisions.order_by("-created_at").first()
        if latest_decision:
            latest_decision.pricing_strategy = pricing
            latest_decision.save(update_fields=["pricing_strategy", "updated_at"])
        reasons.extend(preflight.reason_codes)
        decision = "APPLY" if preflight.allowed else "WATCH"
        job.jobscore.decision = decision
        job.jobscore.reason_codes = list(dict.fromkeys(reasons))
        job.jobscore.save(update_fields=["decision", "reason_codes", "updated_at"])
        scored += 1
        if decision == "APPLY":
            apply_ready += 1
    return {"scored": scored, "apply_ready": apply_ready}


def _application_message(job: Job) -> str:
    return (
        f"I can complete '{job.title}' against the stated requirements, validate the deliverable before submission, "
        "and respond to revision requests through the platform."
    )[:2000]


def attempt_profitable_applications(adapter: AgentGigsAdapter | None = None, limit: int | None = None) -> dict:
    adapter = adapter or configured_adapter()
    if not _auto_apply_enabled():
        return {"attempted": 0, "submitted": 0, "blocked": 0, "disabled": True}
    max_cycle = limit if limit is not None else int(os.getenv("AGENTGIGS_MAX_APPLICATIONS_PER_CYCLE", "2"))
    max_cycle = max(0, min(int(max_cycle), 20))
    attempted = submitted = blocked = 0
    jobs = Job.objects.select_related("marketplace", "jobscore").filter(
        marketplace__slug="agentgigs",
        state=Job.State.EXPECTED,
        jobscore__decision="APPLY",
        applications__isnull=True,
    ).order_by("-jobscore__expected_profit_per_minute")[:max_cycle]
    for job in jobs:
        attempted += 1
        gate = acquisition_decision(job)
        preflight = run_acquisition_preflight(job)
        if not gate.allowed or not preflight.allowed:
            blocked += 1
            continue
        offer = job.jobscore.recommended_offer or recommended_offer(job)
        try:
            acquire_profitable_job(
                adapter=adapter,
                job_id=job.id,
                node_id="VPS1",
                action="APPLY",
                offered_price=offer,
                message=_application_message(job),
            )
            submitted += 1
        except AcquisitionError:
            blocked += 1
    return {"attempted": attempted, "submitted": submitted, "blocked": blocked, "disabled": False}


def sync_applications(adapter: AgentGigsAdapter | None = None) -> dict:
    adapter = adapter or configured_adapter()
    market = ensure_marketplace()
    payload = adapter.applications(limit=100)
    updated = awarded = 0
    for row in payload.get("applications", []):
        if not isinstance(row, dict):
            continue
        remote_id = str(row.get("id") or "")
        nested_job = row.get("job") if isinstance(row.get("job"), dict) else {}
        external_job_id = str(row.get("job_id") or nested_job.get("id") or "")
        if not external_job_id:
            continue
        job = Job.objects.filter(marketplace=market, external_id=external_job_id).first()
        if not job:
            continue
        app = None
        if remote_id:
            app = Application.objects.filter(job=job, remote_reference=remote_id).order_by("-created_at").first()
        if not app:
            # Reconcile a locally submitted attempt whose remote reference was lost
            # only when there is exactly one candidate for this job.
            candidates = list(Application.objects.filter(job=job).order_by("-created_at")[:2])
            if len(candidates) == 1:
                app = candidates[0]
                if remote_id and not app.remote_reference:
                    app.remote_reference = remote_id
        if not app:
            continue
        status = str(row.get("status") or "UNKNOWN").upper()
        changed = []
        if app.status != status:
            app.status = status
            changed.append("status")
        if remote_id and not app.remote_reference:
            app.remote_reference = remote_id
            changed.append("remote_reference")
        if changed:
            app.save(update_fields=[*changed, "updated_at"])
            updated += 1
        decision = job.opportunity_decisions.select_related("pricing_strategy").exclude(pricing_strategy=None).order_by("-created_at").first()
        if decision and decision.pricing_strategy and status in {"FUNDED", "ACCEPTED", "REJECTED", "DECLINED", "EXPIRED"}:
            decision.pricing_strategy.outcome = "WON" if status in {"FUNDED", "ACCEPTED"} else "LOST"
            decision.pricing_strategy.save(update_fields=["outcome", "updated_at"])
        if status == "FUNDED" and job.state == Job.State.EXPECTED:
            transition_job(job.id, Job.State.AWARDED, actor="agentgigs-sync", metadata={"application_id": remote_id})
            awarded += 1
    return {"updated": updated, "awarded": awarded, "remote_counts": payload.get("counts", {})}


def sync_messages(job: Job, adapter: AgentGigsAdapter | None = None) -> int:
    adapter = adapter or configured_adapter()
    opportunity = adapter.normalize_job(job.normalized_payload)
    count = 0
    for row in adapter.get_messages(opportunity):
        if not isinstance(row, dict):
            continue
        remote_id = str(row.get("id") or row.get("message_id") or "")
        body = str(row.get("message") or "")
        created = str(row.get("created_at") or "")
        content_hash = hashlib.sha256(f"{remote_id}|{created}|{body}".encode()).hexdigest()
        _, was_created = JobMessage.objects.get_or_create(
            job=job,
            remote_id=remote_id,
            content_hash=content_hash,
            defaults={
                "source": "agentgigs",
                "direction": "IN",
                "content": body,
                "action_required": False,
            },
        )
        count += int(was_created)
    return count


def sync_job(job: Job, adapter: AgentGigsAdapter | None = None, include_messages: bool = True) -> dict:
    adapter = adapter or configured_adapter()
    opportunity = adapter.normalize_job(job.normalized_payload)
    details = adapter.get_status(opportunity)
    decision = details_decision(details)
    if decision.awarded and job.state == Job.State.EXPECTED:
        transition_job(job.id, Job.State.AWARDED, actor="agentgigs-sync")
        job.refresh_from_db()
    if decision.submitted and job.state == Job.State.EXECUTING:
        transition_job(job.id, Job.State.SUBMITTED, actor="agentgigs-sync")
        Submission.objects.filter(job=job, status="UNKNOWN_REMOTE_STATE").update(status="SUBMITTED", response=details)
        job.refresh_from_db()
    if include_messages and job.state in {Job.State.AWARDED, Job.State.EXECUTING, Job.State.SUBMITTED, Job.State.ACCEPTED, Job.State.PAYOUT_PENDING}:
        try:
            sync_messages(job, adapter)
        except AgentGigsError:
            pass
    return details


def _job_from_event(market: Marketplace, parsed: AgentGigsWebhook) -> Job | None:
    if not parsed.external_job_id:
        return None
    return Job.objects.filter(marketplace=market, external_id=parsed.external_job_id).first()


def _approval_amount(job: Job, adapter: AgentGigsAdapter) -> tuple[Decimal, Decimal, Decimal] | None:
    app = Application.objects.filter(job=job, status__in=["FUNDED", "ACCEPTED", "SUBMITTED"]).order_by("-created_at").first()
    if not app or app.offered_price is None:
        return None
    calculator = adapter.earnings(calculate_cents=int((app.offered_price * 100).quantize(Decimal("1"))))
    return authoritative_payout_from_earnings(calculator)


def _claim_webhook_event(event_id: int) -> WebhookEvent | None:
    with transaction.atomic():
        event = WebhookEvent.objects.select_for_update().select_related("marketplace").get(pk=event_id)
        if event.status == "PROCESSED":
            return None
        now = timezone.now()
        if (
            event.status == "PROCESSING"
            and event.last_attempt_at
            and event.last_attempt_at > now - timedelta(seconds=WEBHOOK_PROCESSING_STALE_SECONDS)
        ):
            return None
        if event.attempt_count >= MAX_WEBHOOK_ATTEMPTS:
            return None
        event.status = "PROCESSING"
        event.attempt_count += 1
        event.last_attempt_at = now
        event.error_code = ""
        event.save(update_fields=["status", "attempt_count", "last_attempt_at", "error_code", "updated_at"])
        return event


def _finish_webhook_event(event_id: int, *, success: bool, error_code: str = "") -> WebhookEvent:
    with transaction.atomic():
        event = WebhookEvent.objects.select_for_update().get(pk=event_id)
        event.status = "PROCESSED" if success else "FAILED"
        event.processed_at = timezone.now() if success else None
        event.error_code = error_code[:120]
        event.save(update_fields=["status", "processed_at", "error_code", "updated_at"])
        return event


def _ensure_pending_payout_for_release(job: Job, market: Marketplace, adapter: AgentGigsAdapter) -> Payout | None:
    payout = Payout.objects.filter(job=job, currency="USD").first()
    if payout and payout.state == Payout.State.PAYOUT_PENDING:
        return payout
    if not (market.payout_ready and market.south_africa_verified):
        return None
    amounts = _approval_amount(job, adapter)
    if not amounts:
        return None
    gross, fee, _net = amounts
    if payout is None and job.state in {Job.State.SUBMITTED, Job.State.ACCEPTED}:
        payout = record_payout_state(job_id=job.id, target_state=Payout.State.EARNED, gross=gross, fee=fee)
        job.refresh_from_db()
    if payout is None:
        payout = Payout.objects.filter(job=job, currency="USD").first()
    if payout and payout.state == Payout.State.EARNED and job.state == Job.State.ACCEPTED:
        payout = record_payout_state(job_id=job.id, target_state=Payout.State.PAYOUT_PENDING, gross=payout.gross, fee=payout.fee)
    return payout


def process_webhook_event(event_id: int, adapter: AgentGigsAdapter | None = None) -> WebhookEvent:
    adapter = adapter or configured_adapter()
    event = _claim_webhook_event(event_id)
    if event is None:
        return WebhookEvent.objects.get(pk=event_id)
    market = event.marketplace
    parsed = AgentGigsWebhook(event=event.event_type, timestamp=event.occurred_at_remote, data=event.payload.get("data", {}))
    job = _job_from_event(market, parsed)
    decision = webhook_decision(parsed.event)
    try:
        # External AgentGigs calls happen outside the WebhookEvent row transaction.
        if parsed.event == "job.available":
            pass
        elif job is None:
            raise ValueError("webhook job is not known locally yet")
        elif parsed.event == "job.accepted":
            # Application selection is not awarded work. sync_job only promotes
            # when details prove funded escrow/in_progress assignment.
            sync_job(job, adapter)
        elif decision.revision_required:
            message = str(parsed.data.get("message") or parsed.data.get("revision_message") or "Revision requested via AgentGigs")
            Revision.objects.get_or_create(
                job=job,
                source_event_key=event.event_key,
                defaults={"message": message, "status": "REQUIRED"},
            )
            if job.state == Job.State.SUBMITTED:
                transition_job(job.id, Job.State.EXECUTING, actor="agentgigs-webhook", metadata={"event_id": event.id})
        elif decision.approved:
            amounts = _approval_amount(job, adapter)
            if amounts and market.payout_ready and market.south_africa_verified and job.state == Job.State.SUBMITTED:
                gross, fee, _net = amounts
                record_payout_state(job_id=job.id, target_state=Payout.State.EARNED, gross=gross, fee=fee)
                record_payout_state(job_id=job.id, target_state=Payout.State.PAYOUT_PENDING, gross=gross, fee=fee)
            else:
                if job.state == Job.State.SUBMITTED:
                    transition_job(job.id, Job.State.ACCEPTED, actor="agentgigs-webhook", metadata={"event_id": event.id})
                Alert.objects.create(
                    severity="WARNING",
                    alert_type="PAYOUT_RECONCILIATION_REQUIRED",
                    message="AgentGigs approved the job but payout economics/readiness are not fully verified.",
                    metadata={"job_id": str(job.id), "event_id": event.id},
                )
        elif decision.payment_released:
            payout = _ensure_pending_payout_for_release(job, market, adapter)
            if payout and payout.state == Payout.State.PAYOUT_PENDING:
                record_payout_state(
                    job_id=job.id,
                    target_state=Payout.State.SETTLED,
                    gross=payout.gross,
                    fee=payout.fee,
                    external_reference=str(parsed.data.get("payment_id") or parsed.data.get("transfer_id") or ""),
                )
            else:
                Alert.objects.create(
                    severity="CRITICAL",
                    alert_type="SETTLEMENT_RECONCILIATION_REQUIRED",
                    message="AgentGigs reported payment release without a verified local pending payout.",
                    metadata={"job_id": str(job.id), "event_id": event.id},
                )
        finished = _finish_webhook_event(event.id, success=True)
        AuditEvent.objects.create(
            event_type="agentgigs.webhook_processed",
            actor="agentgigs-webhook",
            metadata={"webhook_event_id": event.id, "event": event.event_type, "job_id": str(job.id) if job else None},
        )
        return finished
    except Exception as exc:
        failed = _finish_webhook_event(event.id, success=False, error_code=exc.__class__.__name__)
        AuditEvent.objects.create(
            severity="WARNING",
            event_type="agentgigs.webhook_failed",
            actor="agentgigs-webhook",
            metadata={
                "webhook_event_id": event.id,
                "event": event.event_type,
                "job_id": str(job.id) if job else None,
                "attempt": failed.attempt_count,
                "error_code": exc.__class__.__name__,
            },
        )
        raise


def process_pending_webhooks(adapter: AgentGigsAdapter | None = None, limit: int = 100) -> dict:
    adapter = adapter or configured_adapter()
    processed = failed = skipped = 0
    stale_before = timezone.now() - timedelta(seconds=WEBHOOK_PROCESSING_STALE_SECONDS)
    eligible = Q(status__in=["RECEIVED", "FAILED"]) | Q(status="PROCESSING", last_attempt_at__lt=stale_before)
    ids = list(
        WebhookEvent.objects.filter(marketplace__slug="agentgigs", attempt_count__lt=MAX_WEBHOOK_ATTEMPTS)
        .filter(eligible)
        .order_by("created_at")
        .values_list("id", flat=True)[: max(1, min(int(limit), 500))]
    )
    for event_id in ids:
        try:
            event = process_webhook_event(event_id, adapter)
            if event.status == "PROCESSED":
                processed += 1
            else:
                skipped += 1
        except Exception:
            failed += 1
    return {"processed": processed, "failed": failed, "skipped": skipped}


def run_cycle(adapter: AgentGigsAdapter | None = None, limit: int = 100) -> dict:
    """One bounded AgentGigs control cycle, with revenue protection first."""
    adapter = adapter or configured_adapter()
    # P0: revisions, approvals and payment release before chasing new work.
    webhooks = process_pending_webhooks(adapter, limit=limit)
    applications = sync_applications(adapter)
    active = {"synced": 0, "failed": 0}
    jobs = Job.objects.filter(
        marketplace__slug="agentgigs",
        state__in=[Job.State.EXPECTED, Job.State.AWARDED, Job.State.EXECUTING, Job.State.SUBMITTED, Job.State.ACCEPTED, Job.State.PAYOUT_PENDING],
    ).order_by("updated_at")[: max(1, min(int(limit), 500))]
    for job in jobs:
        try:
            sync_job(job, adapter)
            active["synced"] += 1
        except AgentGigsError:
            active["failed"] += 1

    capacity = capture_capacity(persist=True)
    responsive_limit = discovery_limit(limit, UtilizationState(capacity.utilization_state))
    market = sync_market(adapter, discover_limit=responsive_limit)
    scoring = score_open_jobs(limit=responsive_limit)
    acquisition = attempt_profitable_applications(adapter)
    intelligence = refresh_profit_intelligence()
    return {
        "webhooks": webhooks,
        "applications": applications,
        "active": active,
        "market": market,
        "scoring": scoring,
        "acquisition": acquisition,
        "profit_intelligence": intelligence,
    }
