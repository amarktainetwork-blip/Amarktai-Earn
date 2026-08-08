from __future__ import annotations

from django.db import transaction
from django.db.models import Max

from control.models import Artifact, AuditEvent, Execution, Job, Submission
from control.services.jobs import transition_job
from control.services.locks import JobLockUnavailable, acquire_job_lock, release_job_lock
from control.services.workload_policy import require_allowed
from markets.base import MarketAdapter


class SubmissionError(RuntimeError):
    pass


def _remote_reference(payload: dict) -> str:
    for key in ("id", "submission_id", "deliverable_id", "remote_id"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def submit_qa_passed_job(*, adapter: MarketAdapter, job_id, node_id: str = "VPS1", notes: str = "Completed and independently QA verified.") -> Submission:
    job = Job.objects.select_related("marketplace").get(pk=job_id)
    if adapter.slug != job.marketplace.slug:
        raise SubmissionError("adapter does not match job marketplace")
    try:
        require_allowed(job)
    except ValueError as exc:
        raise SubmissionError(str(exc)) from exc
    if job.state == Job.State.SUBMITTED:
        existing = Submission.objects.filter(job=job, status="SUBMITTED").order_by("-version").first()
        if existing:
            return existing
    if job.state != Job.State.EXECUTING:
        raise SubmissionError(f"job must be EXECUTING before submission, got {job.state}")

    execution = Execution.objects.filter(job=job, status="QA_PASSED").order_by("-attempt").first()
    if not execution:
        raise SubmissionError("no QA-passed execution is available")
    artifact = Artifact.objects.filter(job=job, execution=execution).order_by("created_at").first()
    if not artifact:
        raise SubmissionError("QA-passed execution has no persisted artifact")

    lock = acquire_job_lock(job.id, node_id=node_id, lease_seconds=180)
    submission = None
    try:
        with transaction.atomic():
            active = Submission.objects.select_for_update().filter(
                job=job, status__in=["SUBMITTING", "UNKNOWN_REMOTE_STATE", "SUBMITTED"]
            ).order_by("-version").first()
            if active:
                if active.status == "SUBMITTED":
                    return active
                raise SubmissionError(f"submission already requires reconciliation: {active.status}")
            version = (Submission.objects.filter(job=job).aggregate(v=Max("version"))["v"] or 0) + 1
            submission = Submission.objects.create(
                job=job,
                artifact=artifact,
                version=version,
                status="SUBMITTING",
            )

        opportunity = adapter.normalize_job(job.normalized_payload)
        payload = adapter.submit(
            opportunity,
            {
                "path": artifact.path,
                "url": artifact.url,
                "sha256": artifact.sha256,
                "notes": notes,
            },
        )
        submission.remote_id = _remote_reference(payload)
        submission.response = payload
        submission.status = "SUBMITTED"
        submission.save(update_fields=["remote_id", "response", "status", "updated_at"])
        transition_job(job.id, Job.State.SUBMITTED, actor="submission", metadata={"submission_id": submission.id})
        AuditEvent.objects.create(
            event_type="job.submitted",
            actor="submission",
            metadata={"job_id": str(job.id), "submission_id": submission.id, "market": adapter.slug},
        )
        return submission
    except Exception as exc:
        if submission is not None and submission.status == "SUBMITTING":
            submission.status = "UNKNOWN_REMOTE_STATE"
            submission.response = {"error_code": exc.__class__.__name__}
            submission.save(update_fields=["status", "response", "updated_at"])
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="job.submission_unknown_remote_state",
                actor="submission",
                metadata={"job_id": str(job.id), "submission_id": submission.id, "error_code": exc.__class__.__name__},
            )
        raise
    finally:
        try:
            release_job_lock(job.id, node_id=node_id, fencing_token=lock.fencing_token)
        except JobLockUnavailable:
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="job.lock_release_stale",
                actor="submission",
                metadata={"job_id": str(job.id), "node": node_id},
            )
