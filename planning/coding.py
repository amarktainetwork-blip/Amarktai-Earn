from __future__ import annotations

import os
from django.db import transaction

from control.models import AuditEvent, Job
from control.services.github_repos import GitHubRepositoryError, ensure_repository_snapshot, repository_ref
from planning.models import RepositorySnapshot, WorkPlan
from workers.registry import WorkerRegistryError, operation_spec


class CodingPlanningError(RuntimeError):
    pass


_CODING_TERMINAL = {
    WorkPlan.Status.SUBMITTED,
    WorkPlan.Status.QA_PASSED,
    WorkPlan.Status.SUBMITTING,
    WorkPlan.Status.SUBMISSION_RECONCILIATION,
    WorkPlan.Status.EXECUTING,
    WorkPlan.Status.QUEUED,
    WorkPlan.Status.FAILED,
}


def _instruction_parts(job: Job) -> list[str]:
    raw = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    fields = [job.title]
    for key in ("description", "requirements", "instructions", "task", "deliverables"):
        value = raw.get(key)
        if isinstance(value, str):
            fields.append(value)
        elif isinstance(value, list):
            fields.extend(str(item) for item in value if isinstance(item, (str, int, float)))
    return [str(value).strip() for value in fields if str(value).strip()]


def _instruction_text(job: Job) -> str:
    return " ".join(_instruction_parts(job)).casefold()


def _explicit_test_command(job: Job) -> str:
    raw = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    for key in ("test_command", "testCommand", "ci_command", "ciCommand", "verification_command", "verificationCommand"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:4000]
    return ""


def coding_operation(job: Job) -> str | None:
    text = _instruction_text(job)
    test_only = any(term in text for term in (
        "run tests", "run the tests", "test suite", "ci check", "continuous integration", "execute tests", "verify the tests",
    ))
    heavy = any(term in text for term in (
        "refactor", "multi-file", "multiple files", "implement feature", "build feature", "architecture change",
        "schema migration", "database migration", "large change", "complex change",
    ))
    small = any(term in text for term in (
        "fix bug", "bug fix", "patch", "small change", "update code", "change code", "modify code", "add validation",
        "write tests", "add tests", "fix failing test", "fix tests", "implement a function",
    ))
    if not (test_only or heavy or small):
        return None
    if test_only and not (heavy or small):
        return "run_repository_tests"
    if heavy:
        return "code_change_heavy"
    return "code_change_small"


def _set_plan(job: Job, *, operation: str = "", input_spec: dict | None = None, reasons: list[str] | None = None) -> WorkPlan:
    reasons = list(reasons or [])
    with transaction.atomic():
        locked = Job.objects.select_for_update().get(pk=job.pk)
        if locked.state not in {Job.State.CLAIMED, Job.State.AWARDED, Job.State.EXECUTING}:
            raise CodingPlanningError(f"job is not in a plannable acquired state: {locked.state}")
        plan, _ = WorkPlan.objects.select_for_update().get_or_create(job=locked)
        if plan.status in _CODING_TERMINAL:
            return plan
        worker_class = ""
        if operation:
            try:
                worker_class = operation_spec(operation).worker_class
            except WorkerRegistryError:
                reasons.append("WORKER_OPERATION_NOT_REGISTERED")
        plan.worker_class = worker_class
        plan.operation = operation if worker_class else ""
        plan.input_spec = dict(input_spec or {}) if worker_class else {}
        plan.status = WorkPlan.Status.READY if worker_class and not reasons else WorkPlan.Status.BLOCKED
        plan.planner_version = "coding-deterministic-v1"
        plan.reason_codes = reasons
        plan.max_repair_attempts = max(0, min(int(os.getenv("MAX_DETERMINISTIC_REPAIR_ATTEMPTS", "1")), 3))
        plan.last_error_code = ""
        plan.save()
        AuditEvent.objects.create(
            event_type="job.coding_plan_ready" if plan.status == WorkPlan.Status.READY else "job.coding_plan_blocked",
            actor="coding-planner",
            metadata={"job_id": str(locked.id), "plan_id": plan.id, "operation": plan.operation, "reason_codes": reasons},
        )
        return plan


def prepare_coding_plan(job_id, *, stage_repository: bool = True, session=None) -> WorkPlan | None:
    job = Job.objects.get(pk=job_id)
    operation = coding_operation(job)
    if operation is None:
        return None
    if os.getenv("SANDBOX_CODING_ENABLED", "0") != "1":
        return _set_plan(job, reasons=["SANDBOX_CODING_DISABLED"])
    test_command = _explicit_test_command(job)
    if not test_command:
        return _set_plan(job, reasons=["TEST_COMMAND_NOT_EXPLICIT"])
    try:
        ref = repository_ref(job)
    except GitHubRepositoryError as exc:
        AuditEvent.objects.create(
            severity="WARNING",
            event_type="job.repository_reference_rejected",
            actor="coding-planner",
            metadata={"job_id": str(job.id), "error_code": exc.__class__.__name__},
        )
        return _set_plan(job, reasons=["REPOSITORY_NOT_STAGED"])
    if ref is None:
        return _set_plan(job, reasons=["REPOSITORY_NOT_STAGED"])

    snapshot = RepositorySnapshot.objects.filter(job=job, status=RepositorySnapshot.Status.VERIFIED).first()
    if stage_repository and snapshot is None:
        try:
            snapshot = ensure_repository_snapshot(job.id, session=session)
        except Exception as exc:
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="job.repository_snapshot_failed",
                actor="coding-planner",
                metadata={"job_id": str(job.id), "error_code": exc.__class__.__name__},
            )
            return _set_plan(job, reasons=["REPOSITORY_NOT_STAGED"])
    if snapshot is None or snapshot.status != RepositorySnapshot.Status.VERIFIED or not snapshot.path:
        return _set_plan(job, reasons=["REPOSITORY_NOT_STAGED"])

    spec = {
        "operation": operation,
        "repository_path": snapshot.path,
        "repository_snapshot_id": snapshot.id,
        "repository_url": snapshot.repository_url,
        "repository_commit": snapshot.commit_sha,
        "instructions": "\n".join(_instruction_parts(job)),
        "test_command": test_command,
    }
    return _set_plan(job, operation=operation, input_spec=spec)


def dispatch_coding_jobs(*, marketplace_slug: str | None = None, limit: int = 50) -> dict[str, int]:
    from planning.services import _queue_execution

    query = Job.objects.filter(state__in=[Job.State.CLAIMED, Job.State.AWARDED])
    if marketplace_slug:
        query = query.filter(marketplace__slug=marketplace_slug)
    queued = blocked = failed = skipped = 0
    for job in query.order_by("updated_at")[: max(1, min(int(limit), 200))]:
        if coding_operation(job) is None:
            skipped += 1
            continue
        try:
            plan = prepare_coding_plan(job.id, stage_repository=True)
            if plan is None:
                skipped += 1
            elif plan.status == WorkPlan.Status.READY:
                queued += int(_queue_execution(plan))
            elif plan.status == WorkPlan.Status.BLOCKED:
                blocked += 1
            elif plan.status == WorkPlan.Status.FAILED:
                failed += 1
        except Exception as exc:
            failed += 1
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="job.coding_dispatch_failed",
                actor="coding-planner",
                metadata={"job_id": str(job.id), "error_code": exc.__class__.__name__},
            )
    return {"queued": queued, "blocked": blocked, "failed": failed, "skipped": skipped}
