from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, Job, Submission
from planning.models import JobAsset, WorkPlan
from workers.registry import WorkerRegistryError, operation_spec


class PlanningError(RuntimeError):
    pass


PLANNER_VERSION = "deterministic-v1"


def _inside(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    base = root.resolve()
    return resolved == base or base in resolved.parents


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_local_job_asset(*, job_id, path: str, source: str = "upload", external_id: str = "") -> JobAsset:
    job = Job.objects.get(pk=job_id)
    candidate = Path(path).resolve()
    upload_root = Path(os.getenv("AMARKTAI_UPLOAD_ROOT", "/var/lib/amarktai-earn/uploads")).resolve()
    job_root = Path(os.getenv("AMARKTAI_JOB_ROOT", "/var/lib/amarktai-earn/jobs")).resolve()
    if not (_inside(candidate, upload_root) or _inside(candidate, job_root)):
        raise PlanningError("job asset is outside approved upload/job storage")
    if not candidate.is_file():
        raise PlanningError("job asset file does not exist")
    defaults = {
        "source": source[:40],
        "name": candidate.name,
        "path": str(candidate),
        "sha256": _sha256(candidate),
        "size_bytes": candidate.stat().st_size,
        "mime_type": mimetypes.guess_type(candidate.name)[0] or "application/octet-stream",
        "status": JobAsset.Status.VERIFIED,
        "verified_at": timezone.now(),
    }
    if external_id:
        asset, _ = JobAsset.objects.update_or_create(job=job, external_id=external_id, defaults=defaults)
    else:
        asset = JobAsset.objects.filter(job=job, path=str(candidate)).order_by("-created_at").first()
        if asset:
            for key, value in defaults.items():
                setattr(asset, key, value)
            asset.save()
        else:
            asset = JobAsset.objects.create(job=job, external_id="", **defaults)
    # A new verified input is an explicit operator/adapter action, so it may
    # reopen a previously failed/blocked plan for deterministic re-planning.
    WorkPlan.objects.filter(job=job, status__in=[WorkPlan.Status.FAILED, WorkPlan.Status.BLOCKED]).update(
        status=WorkPlan.Status.BLOCKED,
        reason_codes=["INPUT_ASSET_CHANGED_REPLAN_REQUIRED"],
        last_error_code="",
    )
    AuditEvent.objects.create(
        event_type="job.asset_staged",
        actor="asset-stager",
        metadata={"job_id": str(job.id), "asset_id": asset.id, "source": source, "sha256": asset.sha256},
    )
    return asset


def _instruction_text(job: Job) -> str:
    raw = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    fields = [job.title]
    for key in ("description", "requirements", "instructions", "task", "deliverables"):
        value = raw.get(key)
        if isinstance(value, str):
            fields.append(value)
        elif isinstance(value, list):
            fields.extend(str(item) for item in value if isinstance(item, (str, int, float)))
    return " ".join(fields).casefold()


def _infer_operation(job: Job, asset: JobAsset) -> tuple[str, list[str]]:
    suffix = Path(asset.path).suffix.casefold()
    text = _instruction_text(job)
    if suffix == ".json" and "csv" in text and any(term in text for term in ("convert", "conversion", "to csv")):
        return "json_to_csv", []
    if suffix == ".csv" and any(term in text for term in ("normalize", "normalise", "clean csv", "clean the csv", "trim whitespace", "standardize", "standardise")):
        return "csv_normalize", []
    return "", ["TRANSFORMATION_NOT_UNAMBIGUOUS"]


@transaction.atomic
def plan_awarded_job(job_id) -> WorkPlan:
    job = Job.objects.select_for_update().get(pk=job_id)
    if job.state not in {Job.State.CLAIMED, Job.State.AWARDED, Job.State.EXECUTING}:
        raise PlanningError(f"job is not in a plannable acquired state: {job.state}")
    plan, _ = WorkPlan.objects.select_for_update().get_or_create(job=job)
    if plan.status in {
        WorkPlan.Status.SUBMITTED,
        WorkPlan.Status.QA_PASSED,
        WorkPlan.Status.SUBMITTING,
        WorkPlan.Status.SUBMISSION_RECONCILIATION,
        WorkPlan.Status.EXECUTING,
        WorkPlan.Status.QUEUED,
        WorkPlan.Status.FAILED,
    }:
        return plan

    assets = list(JobAsset.objects.filter(job=job, status=JobAsset.Status.VERIFIED).exclude(path="").order_by("created_at")[:2])
    reasons: list[str] = []
    operation = ""
    input_spec = {}
    if not assets:
        reasons.append("INPUT_ASSET_NOT_STAGED")
    elif len(assets) > 1:
        reasons.append("MULTIPLE_INPUT_ASSETS_AMBIGUOUS")
    else:
        operation, infer_reasons = _infer_operation(job, assets[0])
        reasons.extend(infer_reasons)
        if operation:
            input_spec = {"operation": operation, "source": assets[0].path, "asset_id": assets[0].id}

    if operation:
        try:
            plan.worker_class = operation_spec(operation).worker_class
        except WorkerRegistryError:
            plan.worker_class = ""
            reasons.append("WORKER_OPERATION_NOT_REGISTERED")
    else:
        plan.worker_class = ""
    plan.operation = operation
    plan.input_spec = input_spec
    plan.status = WorkPlan.Status.READY if operation and not reasons else WorkPlan.Status.BLOCKED
    plan.planner_version = PLANNER_VERSION
    plan.reason_codes = reasons
    plan.max_repair_attempts = max(0, min(int(os.getenv("MAX_DETERMINISTIC_REPAIR_ATTEMPTS", "1")), 3))
    plan.last_error_code = ""
    plan.save()
    AuditEvent.objects.create(
        event_type="job.plan_ready" if plan.status == WorkPlan.Status.READY else "job.plan_blocked",
        actor="deterministic-planner",
        metadata={"job_id": str(job.id), "plan_id": plan.id, "operation": operation, "reason_codes": reasons},
    )
    return plan


def _queue_execution(plan: WorkPlan) -> bool:
    from control.queueing import queue
    from control.tasks import execute_work_plan_task

    try:
        queue("p3").enqueue(
            execute_work_plan_task,
            plan.id,
            job_id=f"workplan:execute:{plan.id}:{plan.execution_attempts + 1}",
            result_ttl=86400,
            failure_ttl=604800,
        )
    except Exception as exc:
        AuditEvent.objects.create(
            severity="WARNING",
            event_type="job.plan_queue_failed",
            actor="planner",
            metadata={"job_id": str(plan.job_id), "plan_id": plan.id, "error_code": exc.__class__.__name__},
        )
        return False
    WorkPlan.objects.filter(
        pk=plan.pk,
        status__in=[WorkPlan.Status.READY, WorkPlan.Status.NEEDS_REPAIR],
    ).update(status=WorkPlan.Status.QUEUED, last_queued_at=timezone.now())
    return True


def reconcile_submission_plans(*, marketplace_slug: str | None = None, limit: int = 100) -> int:
    plans = WorkPlan.objects.select_related("job", "job__marketplace").filter(
        status=WorkPlan.Status.SUBMISSION_RECONCILIATION
    )
    if marketplace_slug:
        plans = plans.filter(job__marketplace__slug=marketplace_slug)
    reconciled = 0
    for plan in plans.order_by("updated_at")[: max(1, min(int(limit), 500))]:
        if plan.job.state == Job.State.SUBMITTED or Submission.objects.filter(job=plan.job, status="SUBMITTED").exists():
            WorkPlan.objects.filter(pk=plan.pk, status=WorkPlan.Status.SUBMISSION_RECONCILIATION).update(
                status=WorkPlan.Status.SUBMITTED,
                submitted_at=plan.submitted_at or timezone.now(),
                last_error_code="",
            )
            reconciled += 1
    return reconciled


def dispatch_awarded_jobs(*, marketplace_slug: str | None = None, limit: int = 50) -> dict:
    reconciled = reconcile_submission_plans(marketplace_slug=marketplace_slug, limit=limit)
    query = Job.objects.filter(state__in=[Job.State.CLAIMED, Job.State.AWARDED])
    if marketplace_slug:
        query = query.filter(marketplace__slug=marketplace_slug)
    queued = blocked = failed = 0
    for job in query.order_by("updated_at")[: max(1, min(int(limit), 200))]:
        try:
            plan = plan_awarded_job(job.id)
            if plan.status == WorkPlan.Status.READY:
                queued += int(_queue_execution(plan))
            elif plan.status == WorkPlan.Status.BLOCKED:
                blocked += 1
            elif plan.status == WorkPlan.Status.FAILED:
                failed += 1
        except Exception as exc:
            failed += 1
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="job.planning_failed",
                actor="planner",
                metadata={"job_id": str(job.id), "error_code": exc.__class__.__name__},
            )
    submission_queued = 0
    submission_plans = WorkPlan.objects.select_related("job", "job__marketplace").filter(status=WorkPlan.Status.QA_PASSED)
    if marketplace_slug:
        submission_plans = submission_plans.filter(job__marketplace__slug=marketplace_slug)
    for plan in submission_plans.order_by("updated_at")[: max(1, min(int(limit), 200))]:
        submission_queued += int(_queue_submission(plan))
    return {
        "queued": queued,
        "blocked": blocked,
        "failed": failed,
        "submission_queued": submission_queued,
        "submission_reconciled": reconciled,
    }


def execute_work_plan(plan_id: int) -> WorkPlan:
    from control.services.execution import execute_registered_job

    with transaction.atomic():
        plan = WorkPlan.objects.select_for_update().select_related("job", "job__marketplace").get(pk=plan_id)
        if plan.status not in {WorkPlan.Status.READY, WorkPlan.Status.QUEUED, WorkPlan.Status.NEEDS_REPAIR}:
            return plan
        repair = plan.status == WorkPlan.Status.NEEDS_REPAIR
        if repair and plan.repair_attempts >= plan.max_repair_attempts:
            plan.status = WorkPlan.Status.BLOCKED
            plan.reason_codes = [*plan.reason_codes, "MAX_REPAIR_ATTEMPTS_REACHED"]
            plan.save(update_fields=["status", "reason_codes", "updated_at"])
            return plan
        plan.execution_attempts += 1
        if repair:
            plan.repair_attempts += 1
        plan.status = WorkPlan.Status.EXECUTING
        plan.last_error_code = ""
        plan.save(update_fields=["execution_attempts", "repair_attempts", "status", "last_error_code", "updated_at"])
        job_id = plan.job_id
        inputs = dict(plan.input_spec)
        worker_id = f"{plan.worker_class}-{str(job_id)[:8]}"
        worker_class = plan.worker_class

    try:
        execution = execute_registered_job(
            job_id=job_id,
            worker_id=worker_id,
            inputs=inputs,
            allow_repair=repair,
            expected_worker_class=worker_class,
        )
    except Exception as exc:
        WorkPlan.objects.filter(pk=plan_id).update(status=WorkPlan.Status.FAILED, last_error_code=exc.__class__.__name__[:120])
        raise

    plan = WorkPlan.objects.get(pk=plan_id)
    if execution.status == "QA_PASSED":
        plan.status = WorkPlan.Status.QA_PASSED
        plan.reason_codes = []
        plan.save(update_fields=["status", "reason_codes", "updated_at"])
        if plan.job.marketplace.slug == "agentgigs":
            _queue_submission(plan)
    elif execution.status == "NEEDS_REPAIR":
        if plan.repair_attempts >= plan.max_repair_attempts:
            plan.status = WorkPlan.Status.BLOCKED
            plan.reason_codes = ["MAX_REPAIR_ATTEMPTS_REACHED"]
            plan.save(update_fields=["status", "reason_codes", "updated_at"])
        else:
            plan.status = WorkPlan.Status.NEEDS_REPAIR
            plan.reason_codes = ["DETERMINISTIC_QA_FAILED"]
            plan.save(update_fields=["status", "reason_codes", "updated_at"])
            _queue_execution(plan)
    return WorkPlan.objects.get(pk=plan_id)


def _queue_submission(plan: WorkPlan) -> bool:
    from control.queueing import queue
    from control.tasks import submit_work_plan_task

    try:
        queue("p0").enqueue(
            submit_work_plan_task,
            plan.id,
            job_id=f"workplan:submit:{plan.id}",
            result_ttl=86400,
            failure_ttl=604800,
        )
        WorkPlan.objects.filter(pk=plan.pk, status=WorkPlan.Status.QA_PASSED).update(status=WorkPlan.Status.SUBMITTING, last_queued_at=timezone.now())
        return True
    except Exception as exc:
        AuditEvent.objects.create(
            severity="WARNING",
            event_type="job.submission_queue_failed",
            actor="planner",
            metadata={"job_id": str(plan.job_id), "plan_id": plan.id, "error_code": exc.__class__.__name__},
        )
        return False


def submit_work_plan(plan_id: int) -> WorkPlan:
    from control.services.agentgigs import configured_adapter
    from control.services.submission import submit_qa_passed_job

    plan = WorkPlan.objects.select_related("job", "job__marketplace").get(pk=plan_id)
    if plan.status == WorkPlan.Status.SUBMITTED:
        return plan
    if plan.status not in {WorkPlan.Status.QA_PASSED, WorkPlan.Status.SUBMITTING}:
        raise PlanningError(f"work plan is not ready for submission: {plan.status}")
    if plan.job.marketplace.slug != "agentgigs":
        plan.status = WorkPlan.Status.BLOCKED
        plan.reason_codes = ["SUBMISSION_ADAPTER_NOT_AUTOMATED"]
        plan.save(update_fields=["status", "reason_codes", "updated_at"])
        return plan
    try:
        submit_qa_passed_job(adapter=configured_adapter(), job_id=plan.job_id)
    except Exception as exc:
        if Submission.objects.filter(job_id=plan.job_id, status="UNKNOWN_REMOTE_STATE").exists():
            plan.status = WorkPlan.Status.SUBMISSION_RECONCILIATION
            plan.last_error_code = exc.__class__.__name__[:120]
            plan.save(update_fields=["status", "last_error_code", "updated_at"])
            return plan
        plan.status = WorkPlan.Status.FAILED
        plan.last_error_code = exc.__class__.__name__[:120]
        plan.save(update_fields=["status", "last_error_code", "updated_at"])
        raise
    plan.status = WorkPlan.Status.SUBMITTED
    plan.submitted_at = timezone.now()
    plan.save(update_fields=["status", "submitted_at", "updated_at"])
    return plan
