from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from control.models import AuditEvent, GenXCall, ModelStat, Payout

ZERO = Decimal("0")


@transaction.atomic
def record_execution_outcome(*, execution, qa_passed: bool, repair_required: bool) -> int:
    """Attach independent QA and commercial evidence to task-scoped model stats once."""
    calls = list(
        GenXCall.objects.select_for_update().filter(
            job_id=execution.job_id,
            created_at__gte=execution.started_at,
            created_at__lte=execution.ended_at or execution.updated_at,
        )
    )
    if not calls:
        return 0
    payout = Payout.objects.filter(job_id=execution.job_id).first()
    authoritative_revenue = payout.net if payout and payout.state == Payout.State.SETTLED else ZERO
    # A passed QA result is not cash. Model economics receive revenue only from
    # the authoritative settlement hook below; this stage records actual costs
    # and quality outcomes without promoting expected job value into revenue.
    attributable_revenue = authoritative_revenue / Decimal(len(calls))
    recorded = 0
    for call in calls:
        metadata = dict(call.requested_metadata or {})
        recorded_ids = list(metadata.get("economic_outcome_execution_ids") or [])
        execution_key = str(execution.id)
        if execution_key in recorded_ids:
            continue
        stat, _ = ModelStat.objects.select_for_update().get_or_create(model=call.model, task_class=call.task_class)
        if qa_passed:
            stat.qa_accepted += 1
            stat.accepted += 1
        else:
            stat.qa_rejected += 1
        if repair_required:
            stat.repair_required += 1
            stat.retry_count += 1
            stat.total_repair_cost += call.cost_equivalent
        stat.revenue += attributable_revenue
        stat.gross_profit += attributable_revenue
        stat.cost_equivalent += call.cost_equivalent
        actual_net = attributable_revenue - call.cost_equivalent
        stat.profit += actual_net
        stat.net_profit += actual_net
        stat.save(update_fields=[
            "qa_accepted", "accepted", "qa_rejected", "repair_required", "retry_count",
            "total_repair_cost", "revenue", "gross_profit", "cost_equivalent", "profit", "net_profit", "updated_at",
        ])
        recorded_ids.append(execution_key)
        metadata["economic_outcome_execution_ids"] = recorded_ids[-20:]
        call.requested_metadata = metadata
        call.save(update_fields=["requested_metadata", "updated_at"])
        recorded += 1
    if recorded:
        AuditEvent.objects.create(
            event_type="genx.execution_economics_learned",
            actor="profit-brain",
            metadata={
                "job_id": str(execution.job_id),
                "execution_id": execution.id,
                "models_updated": recorded,
                "qa_passed": qa_passed,
                "repair_required": repair_required,
                "authoritative_revenue": str(authoritative_revenue),
            },
        )
    return recorded


@transaction.atomic
def record_settlement_outcome(*, payout: Payout) -> int:
    """Attribute settled cash (and later reversals) to completed model calls once."""
    if payout.state not in {Payout.State.SETTLED, Payout.State.REVERSED}:
        return 0
    calls = list(
        GenXCall.objects.select_for_update()
        .filter(job_id=payout.job_id, status="COMPLETED")
        .order_by("created_at", "id")
    )
    if not calls:
        return 0
    payout_key = str(payout.id)
    share = payout.net / Decimal(len(calls))
    changed = 0
    for call in calls:
        metadata = dict(call.requested_metadata or {})
        outcomes = dict(metadata.get("settlement_outcomes") or {})
        previous = str(outcomes.get(payout_key) or "")
        if previous == payout.state:
            continue
        if payout.state == Payout.State.REVERSED and previous != Payout.State.SETTLED:
            continue
        direction = Decimal("1") if payout.state == Payout.State.SETTLED else Decimal("-1")
        stat, _ = ModelStat.objects.select_for_update().get_or_create(model=call.model, task_class=call.task_class)
        stat.revenue += share * direction
        stat.gross_profit += share * direction
        stat.profit += share * direction
        stat.net_profit += share * direction
        if direction > 0:
            stat.deliverable_accepted += 1
        elif stat.deliverable_accepted:
            stat.deliverable_accepted -= 1
        stat.save(update_fields=[
            "revenue", "gross_profit", "profit", "net_profit", "deliverable_accepted", "updated_at",
        ])
        outcomes[payout_key] = payout.state
        metadata["settlement_outcomes"] = outcomes
        call.requested_metadata = metadata
        call.save(update_fields=["requested_metadata", "updated_at"])
        changed += 1
    if changed:
        AuditEvent.objects.create(
            event_type="genx.settled_revenue_attributed" if payout.state == Payout.State.SETTLED else "genx.settled_revenue_reversed",
            actor="treasury",
            metadata={
                "job_id": str(payout.job_id),
                "payout_id": payout.id,
                "models_updated": changed,
                "authoritative_net": str(payout.net),
                "state": payout.state,
            },
        )
    return changed


def model_economics_snapshot(task_class: str) -> list[dict]:
    rows = ModelStat.objects.filter(task_class=task_class).order_by("-net_profit", "-qa_accepted", "model")
    result = []
    for row in rows:
        qa_total = row.qa_accepted + row.qa_rejected
        result.append({
            "model": row.model,
            "task_class": row.task_class,
            "attempts": row.attempts,
            "successful_executions": row.successful_executions,
            "qa_acceptance_probability": str(Decimal(row.qa_accepted + 1) / Decimal(qa_total + 2)),
            "repair_probability": str(Decimal(row.repair_required + 1) / Decimal(row.attempts + 2)),
            "average_actual_credits": str(row.credits / Decimal(row.attempts)) if row.attempts else None,
            "average_latency_ms": row.total_latency_ms // row.attempts if row.attempts else None,
            "net_profit": str(row.net_profit),
            "profit_per_credit": str(row.net_profit / row.credits) if row.credits else None,
        })
    return result
