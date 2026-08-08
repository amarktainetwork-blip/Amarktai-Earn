from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from control.models import (
    Alert,
    Artifact,
    AuditEvent,
    Execution,
    GenXCall,
    Job,
    JobLock,
    RecoveryAction,
    ServiceHeartbeat,
    Submission,
    WebhookEvent,
    Worker,
)
from planning.models import RepositorySnapshot, WorkPlan


def heartbeat(service: str, *, details: dict[str, Any] | None = None) -> ServiceHeartbeat:
    row, _ = ServiceHeartbeat.objects.update_or_create(
        service=service[:80],
        defaults={
            "node_id": os.getenv("NODE_ID", "VPS1")[:120],
            "last_seen_at": timezone.now(),
            "details": details or {},
        },
    )
    return row


def _record(*, key: str, target_type: str, target_id: str, action: str, outcome: str, reason: str, details: dict | None = None) -> bool:
    row, created = RecoveryAction.objects.get_or_create(
        action_key=key[:200],
        defaults={
            "target_type": target_type[:80],
            "target_id": target_id[:160],
            "action": action[:120],
            "outcome": outcome[:32],
            "reason_code": reason[:120],
            "details": details or {},
            "performed_at": timezone.now(),
        },
    )
    if created:
        AuditEvent.objects.create(
            severity="INFO" if outcome == "RECOVERED" else "WARNING",
            event_type="operations.recovery_action",
            actor="watchdog",
            metadata={"recovery_action_id": row.id, "target_type": target_type, "target_id": target_id, "action": action, "outcome": outcome, "reason_code": reason, **(details or {})},
        )
    return created


def _safe_plan_state(plan: WorkPlan) -> tuple[str, list[str]]:
    if plan.job.state in {Job.State.CLAIMED, Job.State.AWARDED}:
        return WorkPlan.Status.READY, ["RECOVERED_LOCAL_QUEUE_STATE"]
    if plan.job.state == Job.State.EXECUTING and plan.repair_attempts < plan.max_repair_attempts:
        return WorkPlan.Status.NEEDS_REPAIR, ["RECOVERED_STALE_EXECUTION"]
    return WorkPlan.Status.BLOCKED, ["RECOVERY_RETRY_NOT_SAFE"]


def recover_persistent_state(*, now=None) -> dict[str, int]:
    now = now or timezone.now()
    worker_before = now - timedelta(seconds=max(60, int(os.getenv("WATCHDOG_WORKER_STALE_SECONDS", "300"))))
    execution_before = now - timedelta(seconds=max(60, int(os.getenv("WATCHDOG_EXECUTION_STALE_SECONDS", "3600"))))
    plan_before = now - timedelta(seconds=max(60, int(os.getenv("WATCHDOG_PLAN_STALE_SECONDS", "1800"))))
    submission_before = now - timedelta(seconds=max(60, int(os.getenv("WATCHDOG_SUBMISSION_STALE_SECONDS", "900"))))
    results = {"workers": 0, "services": 0, "locks": 0, "executions": 0, "plans": 0, "submissions": 0, "webhooks": 0, "unknown_remote": 0}

    for worker in Worker.objects.filter(last_heartbeat__lt=worker_before).exclude(status="OFFLINE"):
        key = f"worker-stale:{worker.id}:{worker.updated_at.isoformat()}"
        Worker.objects.filter(pk=worker.pk).update(status="OFFLINE", current_job=None)
        results["workers"] += int(_record(key=key, target_type="Worker", target_id=worker.id, action="MARK_OFFLINE", outcome="RECOVERED", reason="STALE_WORKER_HEARTBEAT"))

    service_before = now - timedelta(seconds=max(60, int(os.getenv("WATCHDOG_SERVICE_STALE_SECONDS", "300"))))
    for service in ServiceHeartbeat.objects.filter(last_seen_at__lt=service_before).exclude(service="watchdog"):
        key = f"service-stale:{service.service}:{service.last_seen_at.isoformat()}"
        Alert.objects.get_or_create(
            alert_type="STALE_SERVICE_HEARTBEAT",
            status="OPEN",
            message=f"The {service.service} service heartbeat is stale; Compose restart or operator attention may be required.",
            defaults={"severity": "CRITICAL", "metadata": {"service": service.service, "last_seen_at": service.last_seen_at.isoformat()}},
        )
        results["services"] += int(_record(key=key, target_type="ServiceHeartbeat", target_id=service.service, action="RAISE_STALE_SERVICE_ALERT", outcome="BLOCKED", reason="STALE_SERVICE_HEARTBEAT"))

    for lock in JobLock.objects.filter(lease_until__lt=now):
        key = f"job-lock-expired:{lock.job_id}:{lock.fencing_token}"
        target = str(lock.job_id)
        token = lock.fencing_token
        lock.delete()
        results["locks"] += int(_record(key=key, target_type="JobLock", target_id=target, action="RELEASE_EXPIRED_LEASE", outcome="RECOVERED", reason="STALE_JOB_LEASE", details={"fencing_token": token}))

    stale_executions = Execution.objects.select_related("job", "worker").filter(status__in=["QUEUED", "EXECUTING"], updated_at__lt=execution_before)
    for execution in stale_executions:
        key = f"execution-stale:{execution.id}:{execution.updated_at.isoformat()}"
        with transaction.atomic():
            locked = Execution.objects.select_for_update().get(pk=execution.pk)
            if locked.status not in {"QUEUED", "EXECUTING"}:
                continue
            locked.status = "FAILED"
            locked.ended_at = now
            locked.error_code = "STALE_EXECUTION_RECOVERED"
            locked.error_detail = "Watchdog detected an execution without a live owner heartbeat."
            locked.save(update_fields=["status", "ended_at", "error_code", "error_detail", "updated_at"])
            if locked.worker_id:
                Worker.objects.filter(pk=locked.worker_id).update(status="OFFLINE", current_job=None)
            plan = WorkPlan.objects.select_for_update().filter(job_id=locked.job_id).first()
            if plan:
                next_status, reasons = _safe_plan_state(plan)
                plan.status = next_status
                plan.reason_codes = reasons
                plan.last_error_code = "STALE_EXECUTION_RECOVERED"
                plan.save(update_fields=["status", "reason_codes", "last_error_code", "updated_at"])
        results["executions"] += int(_record(key=key, target_type="Execution", target_id=str(execution.id), action="FAIL_AND_RECONCILE_OWNER", outcome="RECOVERED", reason="STALE_EXECUTION"))

    stale_plans = WorkPlan.objects.select_related("job").filter(status__in=[WorkPlan.Status.QUEUED, WorkPlan.Status.EXECUTING], updated_at__lt=plan_before)
    for plan in stale_plans:
        if Execution.objects.filter(job=plan.job, status__in=["QUEUED", "EXECUTING"]).exists():
            continue
        key = f"plan-stale:{plan.id}:{plan.updated_at.isoformat()}"
        next_status, reasons = _safe_plan_state(plan)
        WorkPlan.objects.filter(pk=plan.pk).update(status=next_status, reason_codes=reasons, last_error_code="OWNER_EXECUTION_MISSING")
        results["plans"] += int(_record(key=key, target_type="WorkPlan", target_id=str(plan.id), action="RECONCILE_MISSING_EXECUTION", outcome="RECOVERED" if next_status != WorkPlan.Status.BLOCKED else "BLOCKED", reason="QUEUE_OWNER_MISSING"))

    for plan in WorkPlan.objects.select_related("job").filter(status=WorkPlan.Status.SUBMITTING, updated_at__lt=submission_before):
        key = f"submission-plan-stale:{plan.id}:{plan.updated_at.isoformat()}"
        submission = Submission.objects.filter(job=plan.job).order_by("-version").first()
        if submission and submission.status == "SUBMITTED":
            WorkPlan.objects.filter(pk=plan.pk).update(status=WorkPlan.Status.SUBMITTED, submitted_at=plan.submitted_at or now, last_error_code="")
            outcome, reason = "RECOVERED", "REMOTE_SUBMISSION_ALREADY_RECORDED"
        elif submission:
            if submission.status == "SUBMITTING":
                Submission.objects.filter(pk=submission.pk).update(status="UNKNOWN_REMOTE_STATE", response={"error_code": "WATCHDOG_PROCESS_INTERRUPTED"})
            WorkPlan.objects.filter(pk=plan.pk).update(status=WorkPlan.Status.SUBMISSION_RECONCILIATION, reason_codes=["AMBIGUOUS_REMOTE_SUBMISSION"], last_error_code="UNKNOWN_REMOTE_STATE")
            outcome, reason = "BLOCKED", "AMBIGUOUS_EXTERNAL_MUTATION"
        else:
            WorkPlan.objects.filter(pk=plan.pk).update(status=WorkPlan.Status.QA_PASSED, reason_codes=["RECOVERED_BEFORE_REMOTE_SUBMISSION"], last_error_code="")
            outcome, reason = "RECOVERED", "NO_REMOTE_MUTATION_RECORD"
        results["submissions"] += int(_record(key=key, target_type="WorkPlan", target_id=str(plan.id), action="RECONCILE_SUBMISSION_STATE", outcome=outcome, reason=reason))

    webhook_before = now - timedelta(seconds=max(60, int(os.getenv("WATCHDOG_WEBHOOK_STALE_SECONDS", "600"))))
    for event in WebhookEvent.objects.filter(status="PROCESSING", last_attempt_at__lt=webhook_before):
        key = f"webhook-stale:{event.id}:{event.attempt_count}"
        WebhookEvent.objects.filter(pk=event.pk).update(status="FAILED", error_code="WATCHDOG_PROCESS_INTERRUPTED")
        results["webhooks"] += int(_record(key=key, target_type="WebhookEvent", target_id=str(event.id), action="RETURN_TO_DETERMINISTIC_RECONCILIATION", outcome="RECOVERED", reason="INTERRUPTED_SAFE_OPERATION"))

    ambiguous = GenXCall.objects.filter(status="UNKNOWN_REMOTE_STATE", external_job_id="")
    for call in ambiguous:
        key = f"genx-ambiguous:{call.id}:{call.updated_at.isoformat()}"
        Alert.objects.get_or_create(
            alert_type="GENX_UNKNOWN_REMOTE_STATE",
            status="OPEN",
            metadata__call_id=str(call.id),
            defaults={"severity": "WARNING", "message": "GenX mutation outcome is unknown and has no remote identifier; automatic replay is blocked.", "metadata": {"call_id": str(call.id), "job_id": str(call.job_id) if call.job_id else None}},
        )
        results["unknown_remote"] += int(_record(key=key, target_type="GenXCall", target_id=str(call.id), action="BLOCK_BLIND_REPLAY", outcome="BLOCKED", reason="AMBIGUOUS_EXTERNAL_MUTATION"))
    return results


def _inside(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    base = root.resolve()
    return resolved == base or base in resolved.parents


def _remove(path: Path, *, root: Path) -> int:
    resolved = path.resolve()
    if not _inside(resolved, root) or resolved == root.resolve() or resolved.is_symlink():
        return 0
    if resolved.is_dir():
        shutil.rmtree(resolved)
    elif resolved.is_file():
        resolved.unlink()
    else:
        return 0
    return 1


def cleanup_storage(*, now=None) -> dict[str, int]:
    now = now or timezone.now()
    results = {"part_files": 0, "failed_workspaces": 0, "repositories": 0, "cache": 0, "logs": 0}
    roots = {
        "jobs": Path(os.getenv("AMARKTAI_JOB_ROOT", "/var/lib/amarktai-earn/jobs")).resolve(),
        "uploads": Path(os.getenv("AMARKTAI_UPLOAD_ROOT", "/var/lib/amarktai-earn/uploads")).resolve(),
        "repositories": Path(os.getenv("AMARKTAI_REPO_ROOT", "/var/lib/amarktai-earn/repos")).resolve(),
        "cache": Path(os.getenv("AMARKTAI_CACHE_ROOT", "/var/lib/amarktai-earn/cache")).resolve(),
        "logs": Path(os.getenv("AMARKTAI_LOG_ROOT", "/var/lib/amarktai-earn/logs")).resolve(),
    }
    part_before = now - timedelta(hours=max(1, int(os.getenv("RETENTION_PART_FILE_HOURS", "24"))))
    for root in (roots["jobs"], roots["uploads"], roots["cache"]):
        if not root.exists():
            continue
        for path in root.rglob("*.part"):
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone()) < part_before:
                    results["part_files"] += _remove(path, root=root)
            except OSError:
                continue

    workspace_before = now - timedelta(days=max(1, int(os.getenv("RETENTION_FAILED_WORKSPACE_DAYS", "7"))))
    for execution in Execution.objects.filter(status="FAILED", updated_at__lt=workspace_before).exclude(workspace=""):
        if Artifact.objects.filter(execution=execution).exclude(retention_class__in=["TEMPORARY", "CACHE"]).exists():
            continue
        results["failed_workspaces"] += _remove(Path(execution.workspace), root=roots["jobs"])

    repo_before = now - timedelta(days=max(1, int(os.getenv("RETENTION_FAILED_REPOSITORY_DAYS", "7"))))
    snapshots = RepositorySnapshot.objects.select_related("job").filter(updated_at__lt=repo_before).filter(Q(status=RepositorySnapshot.Status.BLOCKED) | Q(job__state=Job.State.FAILED))
    for snapshot in snapshots.exclude(path=""):
        if WorkPlan.objects.filter(job=snapshot.job, status__in=[WorkPlan.Status.QUEUED, WorkPlan.Status.EXECUTING, WorkPlan.Status.NEEDS_REPAIR]).exists():
            continue
        results["repositories"] += _remove(Path(snapshot.path), root=roots["repositories"])

    for name, env_name, default_days in (("cache", "RETENTION_CACHE_DAYS", 3), ("logs", "RETENTION_OPERATIONAL_LOG_DAYS", 14)):
        root = roots[name]
        before = now - timedelta(days=max(1, int(os.getenv(env_name, str(default_days)))))
        if not root.exists():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone()) < before:
                    results[name] += _remove(path, root=root)
            except OSError:
                continue

    if any(results.values()):
        key = f"storage-cleanup:{now.strftime('%Y%m%d%H')}"
        _record(key=key, target_type="Storage", target_id=os.getenv("NODE_ID", "VPS1"), action="BOUNDED_RETENTION_CLEANUP", outcome="RECOVERED", reason="RETENTION_POLICY", details=results)
    return results


def run_recovery_cycle(*, reconcile_genx: bool = True) -> dict[str, Any]:
    heartbeat("watchdog", details={"phase": "starting"})
    state = recover_persistent_state()
    cleanup = cleanup_storage()
    genx = {"reconciled": 0, "unresolved": 0}
    if reconcile_genx and os.getenv("GENX_API_KEY", "").strip():
        try:
            from gateways.genx.service import GenXGateway
            genx = GenXGateway().reconcile_pending(limit=max(1, int(os.getenv("WATCHDOG_RECONCILE_LIMIT", "100"))))
        except Exception as exc:
            AuditEvent.objects.create(severity="WARNING", event_type="operations.genx_reconciliation_failed", actor="watchdog", metadata={"error_code": exc.__class__.__name__})
    sandbox = {"removed": 0}
    if os.getenv("SANDBOX_CODING_ENABLED", "0") == "1":
        try:
            from sandbox_broker.client import SandboxBrokerClient
            sandbox = SandboxBrokerClient().cleanup(max_age_seconds=max(60, int(os.getenv("WATCHDOG_SANDBOX_STALE_SECONDS", "1800"))))
        except Exception as exc:
            AuditEvent.objects.create(severity="WARNING", event_type="operations.sandbox_cleanup_failed", actor="watchdog", metadata={"error_code": exc.__class__.__name__})
    result = {"state": state, "cleanup": cleanup, "genx": genx, "sandbox": sandbox}
    heartbeat("watchdog", details=result)
    return result
