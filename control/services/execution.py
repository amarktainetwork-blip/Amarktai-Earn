from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from control.models import Artifact, AuditEvent, Execution, Job, QAResult, Worker
from control.services.jobs import transition_job
from control.services.admission import AdmissionDenied, require_admission
from control.services.locks import JobLockUnavailable, acquire_job_lock, release_job_lock, renew_job_lock
from workers.base import WorkRequest
from workers.qa.runtime import run_qa
from workers.registry import WorkerRegistryError, operation_spec
from control.services.workload_policy import require_allowed


class ExecutionError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    base = root.resolve()
    return resolved == base or base in resolved.parents


def _workspace(root: Path, job: Job, attempt: int) -> Path:
    base = root.resolve()
    target = (base / str(job.id) / f"attempt-{attempt}").resolve()
    if not _inside(target, base):
        raise ExecutionError("workspace escaped configured root")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _validated_inputs(inputs: dict, workspace_root: Path, upload_root: Path, *, job: Job) -> dict:
    clean = dict(inputs)
    approved_sources: list[str] = []
    source = clean.get("source")
    if source:
        source_path = Path(str(source)).resolve()
        if not (_inside(source_path, upload_root) or _inside(source_path, workspace_root)):
            raise ExecutionError("input source is outside approved upload/job storage")
        if not source_path.is_file():
            raise ExecutionError("input source file does not exist")
        clean["source"] = str(source_path)
        approved_sources.append(str(source_path))
    sources = clean.get("sources")
    if sources is not None:
        if not isinstance(sources, list) or not sources:
            raise ExecutionError("input sources must be a non-empty list")
        resolved_sources = []
        for raw in sources:
            source_path = Path(str(raw)).resolve()
            if not (_inside(source_path, upload_root) or _inside(source_path, workspace_root)):
                raise ExecutionError("input source is outside approved upload/job storage")
            if not source_path.is_file():
                raise ExecutionError("input source file does not exist")
            resolved_sources.append(str(source_path)); approved_sources.append(str(source_path))
        clean["sources"] = resolved_sources
    if approved_sources:
        from planning.models import JobAsset

        asset_paths = set(JobAsset.objects.filter(job=job, status=JobAsset.Status.VERIFIED, path__in=approved_sources).values_list("path", flat=True))
        artifact_paths = set(Artifact.objects.filter(job=job, path__in=approved_sources).values_list("path", flat=True))
        known = {str(Path(path).resolve()) for path in {*asset_paths, *artifact_paths}}
        if any(path not in known for path in approved_sources):
            raise ExecutionError("input source is not registered to this job")
    repository = clean.get("repository_path")
    if repository:
        repo_root = Path(os.getenv("AMARKTAI_REPO_ROOT", "/var/lib/amarktai-earn/repos")).resolve()
        repo_path = Path(str(repository)).resolve()
        if not _inside(repo_path, repo_root):
            raise ExecutionError("repository snapshot is outside approved repository storage")
        if not repo_path.is_dir():
            raise ExecutionError("repository snapshot directory does not exist")
        from planning.models import RepositorySnapshot
        if not RepositorySnapshot.objects.filter(job=job, path=str(repo_path), status=RepositorySnapshot.Status.VERIFIED).exists():
            raise ExecutionError("repository snapshot is not registered to this job")
        clean["repository_path"] = str(repo_path)
    return clean


def execute_registered_job(
    *,
    job_id,
    worker_id: str,
    inputs: dict,
    node_id: str = "VPS1",
    workspace_root: str | None = None,
    allow_repair: bool = False,
    expected_worker_class: str | None = None,
) -> Execution:
    """Run a registered worker through one persisted execution/QA lifecycle."""
    job = Job.objects.select_related("marketplace").get(pk=job_id)
    permitted_states = {Job.State.CLAIMED, Job.State.AWARDED}
    if allow_repair:
        permitted_states.add(Job.State.EXECUTING)
    if job.state not in permitted_states:
        raise ExecutionError(f"job must be acquired before execution, got {job.state}")
    try:
        require_allowed(job)
    except ValueError as exc:
        raise ExecutionError(str(exc)) from exc

    operation = str(inputs.get("operation") or "")
    try:
        spec = operation_spec(operation)
    except WorkerRegistryError as exc:
        raise ExecutionError(str(exc)) from exc
    if expected_worker_class and spec.worker_class != expected_worker_class:
        raise ExecutionError(f"operation {operation!r} belongs to {spec.worker_class!r}, not {expected_worker_class!r}")
    try:
        expected_storage = 0
        if spec.worker_class == "media":
            try:
                expected_storage = max(1, int(os.getenv("MEDIA_MAX_OUTPUT_BYTES", str(150 * 1024 * 1024))))
            except ValueError:
                expected_storage = 150 * 1024 * 1024
        require_admission(
            purpose="MEDIA" if spec.worker_class == "media" else "WORKPLAN_EXECUTION",
            job=job, operation=operation, expected_storage_bytes=expected_storage,
        )
    except AdmissionDenied as exc:
        raise ExecutionError(str(exc)) from exc

    root = Path(workspace_root or os.getenv("AMARKTAI_JOB_ROOT", "/var/lib/amarktai-earn/jobs"))
    upload_root = Path(os.getenv("AMARKTAI_UPLOAD_ROOT", "/var/lib/amarktai-earn/uploads"))
    lease_seconds = int(os.getenv("JOB_LOCK_LEASE_SECONDS", "1800"))
    clean_inputs = _validated_inputs(inputs, root, upload_root, job=job)
    lock = acquire_job_lock(job.id, node_id=node_id, lease_seconds=lease_seconds)
    execution = None
    worker = None
    try:
        with transaction.atomic():
            worker, _ = Worker.objects.select_for_update().get_or_create(
                id=worker_id,
                defaults={
                    "worker_class": spec.worker_class,
                    "version": spec.version,
                    "node": node_id,
                    "status": "READY",
                },
            )
            if worker.worker_class != spec.worker_class:
                raise ExecutionError(f"worker {worker.id!r} is registered as {worker.worker_class!r}, expected {spec.worker_class!r}")
            if worker.version != spec.version:
                worker.version = spec.version
            next_attempt = (Execution.objects.filter(job=job).aggregate(value=Max("attempt"))["value"] or 0) + 1
            workspace = _workspace(root, job, next_attempt)
            execution = Execution.objects.create(
                job=job,
                worker=worker,
                node_id=node_id,
                attempt=next_attempt,
                status="EXECUTING",
                workspace=str(workspace),
                started_at=timezone.now(),
            )
            worker.status = "EXECUTING"
            worker.current_job = job
            worker.last_heartbeat = timezone.now()
            worker.save(update_fields=["version", "status", "current_job", "last_heartbeat", "updated_at"])
            transition_job(job.id, Job.State.EXECUTING, actor=worker_id, metadata={"execution_id": execution.id, "worker_class": spec.worker_class, "operation": operation})
            if hasattr(job, "internal_opportunity"):
                from control.services.product_factory import mark_internal_execution_started

                mark_internal_execution_started(job=job)

        result = spec.build().execute(WorkRequest(job_id=str(job.id), workspace=Path(execution.workspace), inputs=clean_inputs, worker_id=worker_id, execution_id=execution.id, attempt=execution.attempt))
        lock = renew_job_lock(job.id, node_id=node_id, fencing_token=lock.fencing_token, lease_seconds=lease_seconds)
        if not result.ok:
            Execution.objects.filter(pk=execution.pk).update(
                status="FAILED",
                ended_at=timezone.now(),
                error_code="WORKER_FAILED",
                error_detail=(result.error or f"{spec.worker_class} worker failed")[:4000],
            )
            Worker.objects.filter(pk=worker.pk).update(status="ERROR", current_job=None, last_heartbeat=timezone.now())
            transition_job(job.id, Job.State.FAILED, actor=worker_id, metadata={"execution_id": execution.id, "reason": "worker_failed"})
            raise ExecutionError(result.error or f"{spec.worker_class} worker failed")

        artifact_rows: list[Artifact] = []
        for path in result.artifacts:
            resolved = path.resolve()
            workspace = Path(execution.workspace).resolve()
            if not _inside(resolved, workspace):
                raise ExecutionError("worker artifact escaped execution workspace")
            if not resolved.is_file():
                raise ExecutionError("worker declared a missing artifact")
            artifact_rows.append(
                Artifact.objects.create(
                    job=job,
                    execution=execution,
                    path=str(resolved),
                    sha256=_sha256(resolved),
                    size_bytes=resolved.stat().st_size,
                    mime_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
                )
            )

        if not artifact_rows:
            raise ExecutionError("worker produced no deliverable artifacts")
        qa = run_qa(spec.qa_profile, Path(artifact_rows[0].path), result.evidence)
        QAResult.objects.create(
            job=job,
            execution=execution,
            check_type=qa.check_type,
            passed=qa.passed,
            score=qa.score,
            evidence={"checks": qa.checks, **qa.evidence},
        )
        execution.ended_at = timezone.now()
        status = "QA_PASSED" if qa.passed else "NEEDS_REPAIR"
        Execution.objects.filter(pk=execution.pk).update(
            status=status,
            ended_at=timezone.now(),
            result={
                "worker_class": spec.worker_class,
                "worker_version": spec.version,
                "operation": operation,
                "worker_evidence": result.evidence,
                "qa": {"passed": qa.passed, "check_type": qa.check_type, "checks": qa.checks},
            },
        )
        from planning.acceptance import evaluate_execution_acceptance

        evaluation = evaluate_execution_acceptance(execution.id)
        acceptance_passed = bool(qa.passed and evaluation.submission_ready)
        status = "QA_PASSED" if acceptance_passed else "NEEDS_REPAIR"
        Execution.objects.filter(pk=execution.pk).update(status=status)
        from control.services.genx_economics import record_execution_outcome

        record_execution_outcome(
            execution=execution,
            qa_passed=acceptance_passed,
            repair_required=not acceptance_passed or allow_repair,
        )
        if hasattr(job, "internal_opportunity"):
            from control.services.product_factory import record_internal_execution_outcome

            record_internal_execution_outcome(execution=execution, qa_passed=acceptance_passed)
        Worker.objects.filter(pk=worker.pk).update(
            status="READY" if acceptance_passed else "REPAIRING",
            current_job=None if acceptance_passed else job,
            last_heartbeat=timezone.now(),
        )
        if operation == "synthetic_dataset_generate":
            from control.services.synthetic_data import persist_synthetic_dataset_run

            persist_synthetic_dataset_run(
                job=job, execution=execution, evidence=result.evidence, qa_passed=qa.passed,
            )
        AuditEvent.objects.create(
            event_type="job.qa_passed" if acceptance_passed else "job.qa_failed",
            actor="qa-runtime",
            metadata={
                "job_id": str(job.id),
                "execution_id": execution.id,
                "worker_class": spec.worker_class,
                "operation": operation,
                "qa_profile": spec.qa_profile,
                "checks": qa.checks,
                "acceptance_contract_id": evaluation.contract_id,
                "semantic_state": evaluation.semantic_state,
                "acceptance_reason_codes": evaluation.critical_failures,
            },
        )
        execution.refresh_from_db()
        return execution
    except Exception as exc:
        if execution is not None:
            execution.refresh_from_db()
            if execution.status == "EXECUTING":
                Execution.objects.filter(pk=execution.pk).update(
                    status="FAILED",
                    ended_at=timezone.now(),
                    error_code=exc.__class__.__name__[:120],
                    error_detail=str(exc)[:4000],
                )
        if worker is not None:
            Worker.objects.filter(pk=worker.pk).update(status="ERROR", current_job=None, last_heartbeat=timezone.now())
        fresh = Job.objects.get(pk=job.id)
        if fresh.state == Job.State.EXECUTING:
            transition_job(job.id, Job.State.FAILED, actor=worker_id, metadata={"reason": exc.__class__.__name__})
        raise
    finally:
        try:
            release_job_lock(job.id, node_id=node_id, fencing_token=lock.fencing_token)
        except JobLockUnavailable:
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="job.lock_release_stale",
                actor=worker_id,
                metadata={"job_id": str(job.id), "node": node_id},
            )


def execute_structured_data_job(*, job_id, worker_id: str, inputs: dict, node_id: str = "VPS1", workspace_root: str | None = None, allow_repair: bool = False) -> Execution:
    """Backward-compatible wrapper for the first production worker."""
    return execute_registered_job(
        job_id=job_id,
        worker_id=worker_id,
        inputs=inputs,
        node_id=node_id,
        workspace_root=workspace_root,
        allow_repair=allow_repair,
        expected_worker_class="structured_data",
    )
