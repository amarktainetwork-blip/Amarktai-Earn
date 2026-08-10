from __future__ import annotations

import os

from django.db import transaction

from control.models import AuditEvent, InboundOrder, Job
from control.services.autonomy import AutonomyMode, current_mode
from control.services.jobs import transition_job
from control.services.seller_services import record_inbound_delivery, run_inbound_economic_preflight
from planning.models import WorkPlan
from planning.services import _queue_execution, plan_awarded_job


class InboundControllerError(ValueError):
    pass


def _manual_mode_allowed() -> bool:
    return current_mode() in {AutonomyMode.MANUAL, AutonomyMode.LOW_RISK, AutonomyMode.FULL}


def _auto_mode_allowed() -> bool:
    return current_mode() in {AutonomyMode.LOW_RISK, AutonomyMode.FULL}


@transaction.atomic
def accept_inbound_order(order_id, *, actor: str = "owner", manual: bool = True) -> InboundOrder:
    order = InboundOrder.objects.select_related("job", "marketplace", "listing__offering").select_for_update().get(pk=order_id)
    if order.status == InboundOrder.Status.ACCEPTED and order.job.state in {Job.State.AWARDED, Job.State.EXECUTING}:
        return order
    if order.status != InboundOrder.Status.READY:
        raise InboundControllerError("INBOUND_ORDER_NOT_READY")

    preflight = run_inbound_economic_preflight(order)
    order.refresh_from_db()
    if not preflight.eligible or order.status != InboundOrder.Status.READY:
        raise InboundControllerError("INBOUND_ORDER_PREFLIGHT_NOT_ELIGIBLE")

    if manual:
        if not _manual_mode_allowed():
            raise InboundControllerError("MANUAL_ACCEPT_REQUIRES_MANUAL_OR_HIGHER_MODE")
    else:
        if not _auto_mode_allowed():
            raise InboundControllerError("INBOUND_AUTO_ACCEPT_AUTONOMY_BLOCKED")
        if os.getenv("INBOUND_SERVICE_AUTO_ACCEPT_ENABLED", "0") != "1":
            raise InboundControllerError("INBOUND_SERVICE_AUTO_ACCEPT_DISABLED")

    job = Job.objects.select_for_update().get(pk=order.job_id)
    if job.state == Job.State.EXPECTED:
        transition_job(job.id, Job.State.AWARDED, actor=actor, metadata={"inbound_order_id": str(order.id), "manual": manual})
    elif job.state not in {Job.State.AWARDED, Job.State.EXECUTING}:
        raise InboundControllerError(f"INBOUND_JOB_NOT_ACCEPTABLE:{job.state}")

    order.status = InboundOrder.Status.ACCEPTED
    order.remote_state = "ACCEPTED_LOCAL"
    order.save(update_fields=["status", "remote_state", "updated_at"])
    AuditEvent.objects.create(
        event_type="inbound.order_accepted_manual" if manual else "inbound.order_accepted_automatic",
        actor=str(actor)[:120],
        metadata={
            "order_id": str(order.id),
            "job_id": str(order.job_id),
            "market": order.marketplace.slug,
            "autonomy_mode": current_mode().value,
        },
    )
    order.refresh_from_db()
    return order


def auto_accept_ready_inbound_orders(*, limit: int = 50) -> dict[str, int]:
    if not _auto_mode_allowed() or os.getenv("INBOUND_SERVICE_AUTO_ACCEPT_ENABLED", "0") != "1":
        return {"accepted": 0, "blocked": 0, "skipped": InboundOrder.objects.filter(status=InboundOrder.Status.READY).count()}

    accepted = blocked = 0
    ids = list(InboundOrder.objects.filter(status=InboundOrder.Status.READY).order_by("created_at").values_list("id", flat=True)[: max(1, min(int(limit), 200))])
    for order_id in ids:
        try:
            accept_inbound_order(order_id, actor="revenue-controller", manual=False)
            accepted += 1
        except (InboundControllerError, ValueError):
            blocked += 1
    return {"accepted": accepted, "blocked": blocked, "skipped": 0}


def dispatch_accepted_inbound_orders(*, limit: int = 50) -> dict[str, int]:
    """Plan and queue seller-side orders without entering the AgentGigs submission adapter."""
    queued = blocked = failed = 0
    job_ids = list(
        InboundOrder.objects.filter(
            status=InboundOrder.Status.ACCEPTED,
            job__state__in=[Job.State.AWARDED, Job.State.CLAIMED],
        )
        .order_by("updated_at")
        .values_list("job_id", flat=True)[: max(1, min(int(limit), 200))]
    )
    for job_id in job_ids:
        try:
            plan = plan_awarded_job(job_id)
            if plan.status == WorkPlan.Status.READY:
                if _queue_execution(plan):
                    queued += 1
                else:
                    blocked += 1
            elif plan.status == WorkPlan.Status.BLOCKED:
                blocked += 1
            elif plan.status == WorkPlan.Status.FAILED:
                failed += 1
        except Exception as exc:
            failed += 1
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="inbound.planning_failed",
                actor="revenue-controller",
                metadata={"job_id": str(job_id), "error_code": exc.__class__.__name__},
            )
    return {"queued": queued, "blocked": blocked, "failed": failed, "submission_queued": 0, "submission_reconciled": 0}


def _qa_passed_plan(order: InboundOrder):
    return WorkPlan.objects.filter(job_id=order.job_id, status=WorkPlan.Status.QA_PASSED).order_by("-updated_at").first()


@transaction.atomic
def refresh_inbound_delivery_readiness(*, limit: int = 100) -> dict[str, int]:
    ready = api_delivered = unchanged = 0
    ids = list(
        InboundOrder.objects.filter(status=InboundOrder.Status.ACCEPTED)
        .order_by("updated_at")
        .values_list("id", flat=True)[: max(1, min(int(limit), 500))]
    )
    for order_id in ids:
        order = InboundOrder.objects.select_related("job", "marketplace").select_for_update().get(pk=order_id)
        if not _qa_passed_plan(order):
            unchanged += 1
            continue
        if order.marketplace.slug == "rapidapi":
            if order.job.state == Job.State.EXECUTING:
                transition_job(order.job_id, Job.State.SUBMITTED, actor="rapidapi-provider", metadata={"inbound_order_id": str(order.id)})
            order.refresh_from_db()
            if order.job.state == Job.State.SUBMITTED:
                record_inbound_delivery(
                    order,
                    remote_reference=f"/api/channels/rapidapi/orders/{order.id}",
                    actor="rapidapi-provider",
                )
                api_delivered += 1
            continue
        if order.remote_state != "DELIVERY_READY":
            order.remote_state = "DELIVERY_READY"
            order.save(update_fields=["remote_state", "updated_at"])
            AuditEvent.objects.create(
                event_type="inbound.delivery_ready",
                actor="revenue-controller",
                metadata={"order_id": str(order.id), "job_id": str(order.job_id), "market": order.marketplace.slug},
            )
            ready += 1
        else:
            unchanged += 1
    return {"delivery_ready": ready, "api_delivered": api_delivered, "unchanged": unchanged}


@transaction.atomic
def record_manual_inbound_delivery(order_id, *, remote_reference: str, actor: str = "owner") -> InboundOrder:
    order = InboundOrder.objects.select_related("job", "marketplace").select_for_update().get(pk=order_id)
    if order.marketplace.slug == "rapidapi":
        raise InboundControllerError("RAPIDAPI_DELIVERY_IS_PROVIDER_ENDPOINT")
    if order.status != InboundOrder.Status.ACCEPTED or order.remote_state != "DELIVERY_READY":
        raise InboundControllerError("INBOUND_DELIVERY_NOT_READY")
    if not _qa_passed_plan(order):
        raise InboundControllerError("INBOUND_DELIVERY_REQUIRES_QA_PASS")
    if order.job.state == Job.State.EXECUTING:
        transition_job(order.job_id, Job.State.SUBMITTED, actor=actor, metadata={"inbound_order_id": str(order.id)})
    order.refresh_from_db()
    if order.job.state != Job.State.SUBMITTED:
        raise InboundControllerError("INBOUND_DELIVERY_JOB_NOT_SUBMITTED")
    return record_inbound_delivery(order, remote_reference=remote_reference, actor=actor)


def revenue_controller_cycle(*, limit: int = 50) -> dict:
    return {
        "auto_accept": auto_accept_ready_inbound_orders(limit=limit),
        "dispatch": dispatch_accepted_inbound_orders(limit=limit),
        "delivery": refresh_inbound_delivery_readiness(limit=limit),
        "autonomy_mode": current_mode().value,
    }
