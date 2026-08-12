from __future__ import annotations

from django.db import transaction

from control.models import AuditEvent, Execution, GenXCall, Job, Submission
from planning.models import WorkPlan


class FailedWorkPlanRetryError(RuntimeError):
    pass


@transaction.atomic
def prepare_failed_work_plan_retry(
    plan_id: int,
    *,
    reason: str,
    actor: str = "operator-recovery",
) -> WorkPlan:
    """Reopen a failed acquired job only when replay cannot duplicate a remote mutation.

    FAILED remains terminal for ordinary state transitions. This is an explicit,
    audited operator recovery boundary for a corrected local defect. It preserves
    execution-attempt history and never fabricates QA, GenX, or submission evidence.
    """
    reason = str(reason or "").strip()
    if not reason:
        raise FailedWorkPlanRetryError("retry reason is required")

    plan = (
        WorkPlan.objects.select_for_update()
        .select_related("job")
        .get(pk=plan_id)
    )
    job = Job.objects.select_for_update().get(pk=plan.job_id)

    if plan.status != WorkPlan.Status.FAILED:
        raise FailedWorkPlanRetryError(f"work plan is not FAILED: {plan.status}")
    if job.state != Job.State.FAILED:
        raise FailedWorkPlanRetryError(f"job is not FAILED: {job.state}")
    if Submission.objects.filter(job=job).exists():
        raise FailedWorkPlanRetryError("job has submission history; failed replay is not safe")
    if Execution.objects.filter(job=job, status__in=["QUEUED", "EXECUTING", "NEEDS_REPAIR"]).exists():
        raise FailedWorkPlanRetryError("job still has an active or repairable execution")

    unsafe_calls = GenXCall.objects.filter(job=job).exclude(status__in=["FAILED", "CANCELLED"])
    if unsafe_calls.exists():
        raise FailedWorkPlanRetryError(
            "job has a GenX call with completed, pending, or unknown remote state; reconcile instead of replaying"
        )

    previous_plan_reasons = list(plan.reason_codes or [])
    previous_error = str(plan.last_error_code or "")

    # FAILED stays terminal in the ordinary state machine. A corrected local defect
    # may be reopened only through this explicit operator recovery boundary.
    job.state = Job.State.AWARDED
    job.save(update_fields=["state", "updated_at"])

    plan.status = WorkPlan.Status.READY
    plan.reason_codes = ["OPERATOR_RETRY_AFTER_CORRECTED_LOCAL_DEFECT"]
    plan.last_error_code = ""
    plan.save(update_fields=["status", "reason_codes", "last_error_code", "updated_at"])

    AuditEvent.objects.create(
        event_type="job.failed_reopened",
        actor=actor[:120],
        metadata={
            "job_id": str(job.id),
            "plan_id": plan.id,
            "reason": reason[:500],
            "previous_job_state": Job.State.FAILED,
            "new_job_state": Job.State.AWARDED,
            "previous_plan_status": WorkPlan.Status.FAILED,
            "new_plan_status": WorkPlan.Status.READY,
            "previous_plan_reasons": previous_plan_reasons,
            "previous_error": previous_error,
            "execution_attempts_preserved": plan.execution_attempts,
        },
    )
    return plan


def retry_failed_work_plan(
    plan_id: int,
    *,
    reason: str,
    actor: str = "operator-recovery",
    enqueue: bool = True,
) -> WorkPlan:
    plan = prepare_failed_work_plan_retry(plan_id, reason=reason, actor=actor)
    if not enqueue:
        return plan

    # Import lazily to keep the recovery boundary independent of planner startup.
    from planning.services import _queue_execution

    if not _queue_execution(plan):
        plan.refresh_from_db()
        AuditEvent.objects.create(
            severity="WARNING",
            event_type="job.failed_reopen_queue_failed",
            actor=actor[:120],
            metadata={
                "job_id": str(plan.job_id),
                "plan_id": plan.id,
                "status": plan.status,
                "reason": reason[:500],
            },
        )
        raise FailedWorkPlanRetryError(f"reopened work plan could not be queued: {plan.status}")
    plan.refresh_from_db()
    return plan
