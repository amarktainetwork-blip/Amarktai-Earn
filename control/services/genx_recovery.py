from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import Artifact, AuditEvent, Execution, GenXCall, Job, Worker
from control.services.execution import ExecutionError, _inside, finalize_successful_execution
from gateways.genx.client import GenXError
from gateways.genx.output import (
    decode_text_result_url,
    extract_session_assistant_text,
    extract_session_sources,
    extract_text,
)
from gateways.genx.service import GenXGateway
from planning.models import WorkPlan
from workers.base import WorkRequest
from workers.registry import WorkerRegistryError, operation_spec


class GenXRecoveryError(RuntimeError):
    pass


def _remote_identity(call: GenXCall) -> tuple[str, str, str]:
    metadata = call.requested_metadata or {}
    session_id = str(metadata.get("session_id") or "")
    message_id = str(metadata.get("message_id") or "")
    remote_job_id = str(metadata.get("remote_job_id") or "")
    if not remote_job_id and call.external_job_id and not call.external_job_id.startswith("session:"):
        remote_job_id = call.external_job_id
    if not session_id and call.external_job_id.startswith("session:"):
        session_id = call.external_job_id.split(":", 1)[1]
    return session_id, message_id, remote_job_id


def _extract_completed_text(
    *,
    gateway: GenXGateway,
    call: GenXCall,
    remote_payload: dict[str, Any],
    session_id: str,
    message_id: str,
    remote_job_id: str,
) -> tuple[str, list[str], dict[str, Any]]:
    history: dict[str, Any] = {}
    text = ""
    sources: list[str] = []

    if session_id:
        try:
            history = gateway.client.session_messages(session_id)
        except GenXError:
            history = {}
        if history:
            text = extract_session_assistant_text(
                history,
                job_id=remote_job_id or None,
                message_id=message_id or None,
            )
            sources.extend(extract_session_sources(history))

    if not text:
        text = str((call.requested_metadata or {}).get("assistant_text") or "").strip()
    if not text:
        text = decode_text_result_url(call.result_url)
    if not text:
        text = decode_text_result_url(str(remote_payload.get("result_url") or ""))

    result_payload: dict[str, Any] = {}
    if not text and remote_job_id:
        try:
            result_payload = gateway.client.result(remote_job_id)
        except GenXError:
            result_payload = {}
        if result_payload:
            candidate = extract_text(result_payload)
            if candidate.startswith(("data:", "data/")):
                candidate = decode_text_result_url(candidate)
            text = candidate.strip()
            sources.extend(extract_session_sources(result_payload))

    if not text and remote_job_id:
        try:
            raw = gateway.client.job_file(remote_job_id, max_bytes=1024 * 1024)
            text = raw.decode("utf-8").strip()
        except (GenXError, UnicodeDecodeError):
            text = ""

    if text:
        sources.extend(url.rstrip(".,;") for url in re.findall(r"https://[^\s)\]>]+", text))
    return text, list(dict.fromkeys(sources)), {"session_history": history, "result_payload": result_payload}


def _execution_for_call(call: GenXCall) -> Execution:
    if not call.job_id:
        raise GenXRecoveryError("GenX call is not attached to a job")
    query = Execution.objects.filter(job_id=call.job_id)
    if call.worker_id:
        query = query.filter(worker_id=call.worker_id)
    if call.started_at:
        query = query.filter(started_at__lte=call.started_at)
    execution = query.order_by("-attempt", "-id").first()
    if execution is None:
        raise GenXRecoveryError("no originating execution exists for the completed GenX call")
    if execution.status not in {"EXECUTING", "FAILED", "NEEDS_REPAIR", "QA_PASSED"}:
        raise GenXRecoveryError(f"originating execution is not recoverable: {execution.status}")
    return execution


def _prepare_local_recovery(*, call: GenXCall, execution: Execution, plan: WorkPlan) -> tuple[Job, Worker]:
    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=call.job_id)
        execution = Execution.objects.select_for_update().get(pk=execution.pk)
        plan = WorkPlan.objects.select_for_update().get(pk=plan.pk)
        worker = Worker.objects.select_for_update().get(pk=call.worker_id or execution.worker_id)

        if job.state in {Job.State.SUBMITTED, Job.State.ACCEPTED, Job.State.PAYOUT_PENDING, Job.State.SETTLED}:
            raise GenXRecoveryError(f"job is already beyond execution recovery: {job.state}")
        if plan.status in {WorkPlan.Status.SUBMITTING, WorkPlan.Status.SUBMISSION_RECONCILIATION, WorkPlan.Status.SUBMITTED}:
            raise GenXRecoveryError(f"work plan is already beyond execution recovery: {plan.status}")

        previous_job_state = job.state
        previous_plan_status = plan.status
        previous_execution_status = execution.status

        if job.state != Job.State.EXECUTING:
            job.state = Job.State.EXECUTING
            job.save(update_fields=["state", "updated_at"])
        plan.status = WorkPlan.Status.EXECUTING
        plan.reason_codes = ["RECOVERING_ALREADY_COMPLETED_REMOTE_RESULT"]
        plan.last_error_code = ""
        plan.save(update_fields=["status", "reason_codes", "last_error_code", "updated_at"])
        execution.status = "EXECUTING"
        execution.ended_at = None
        execution.error_code = ""
        execution.error_detail = ""
        result = execution.result if isinstance(execution.result, dict) else {}
        execution.result = {
            **result,
            "recovery": {
                "kind": "ALREADY_COMPLETED_PROVIDER_RESULT",
                "genx_call_id": str(call.id),
                "remote_job_id": str((call.requested_metadata or {}).get("remote_job_id") or call.external_job_id),
                "provider_replayed": False,
            },
        }
        execution.save(update_fields=["status", "ended_at", "error_code", "error_detail", "result", "updated_at"])
        worker.status = "EXECUTING"
        worker.current_job = job
        worker.last_heartbeat = timezone.now()
        worker.save(update_fields=["status", "current_job", "last_heartbeat", "updated_at"])

        AuditEvent.objects.create(
            event_type="genx.completed_execution_recovery_started",
            actor="genx-recovery",
            metadata={
                "call_id": str(call.id),
                "job_id": str(job.id),
                "plan_id": plan.id,
                "execution_id": execution.id,
                "previous_job_state": previous_job_state,
                "previous_plan_status": previous_plan_status,
                "previous_execution_status": previous_execution_status,
                "provider_replayed": False,
            },
        )
    return job, worker


def recover_completed_genx_call(
    call_id,
    *,
    expected_remote_job_id: str = "",
    gateway: GenXGateway | None = None,
) -> dict[str, Any]:
    """Recover one already-submitted GenX call end-to-end without replaying it.

    Remote operations in this function are read-only GETs. The authoritative
    terminal payload is reconciled idempotently, then the originating worker's
    explicit recovery hook materializes the result into the original execution.
    No generate/session/message POST is possible through this path.
    """
    gateway = gateway or GenXGateway()
    call = GenXCall.objects.select_related("job", "worker").get(pk=call_id)
    session_id, message_id, remote_job_id = _remote_identity(call)
    expected_remote_job_id = str(expected_remote_job_id or "").strip()
    if expected_remote_job_id and remote_job_id and remote_job_id != expected_remote_job_id:
        raise GenXRecoveryError(
            f"remote job identity mismatch: stored={remote_job_id} expected={expected_remote_job_id}"
        )
    if not remote_job_id:
        if not session_id:
            raise GenXRecoveryError("GenX call has no authoritative remote job or session identity")
        try:
            history = gateway.client.session_messages(session_id)
        except GenXError as exc:
            raise GenXRecoveryError("could not recover remote job identity from the existing session") from exc
        from gateways.genx.output import session_assistant_job_ids

        discovered = session_assistant_job_ids(history)
        if len(discovered) != 1:
            raise GenXRecoveryError("existing session does not expose one authoritative assistant job identity")
        remote_job_id = discovered[0]
        if expected_remote_job_id and remote_job_id != expected_remote_job_id:
            raise GenXRecoveryError(
                f"recovered remote job identity mismatch: stored={remote_job_id} expected={expected_remote_job_id}"
            )
        metadata = dict(call.requested_metadata or {})
        metadata.update({"session_id": session_id, "remote_job_id": remote_job_id})
        GenXCall.objects.filter(pk=call.pk).update(
            external_job_id=remote_job_id,
            requested_metadata=metadata,
        )
        call.external_job_id = remote_job_id
        call.requested_metadata = metadata

    try:
        remote_payload = gateway.client.job(remote_job_id)
    except GenXError as exc:
        raise GenXRecoveryError("existing remote GenX job could not be retrieved") from exc
    remote_status = str(remote_payload.get("status") or "").upper()
    if remote_status not in {"COMPLETED", "FAILED", "CANCELLED"}:
        raise GenXRecoveryError(f"existing remote GenX job is not terminal: {remote_status or 'UNKNOWN'}")

    call = gateway.reconcile_remote_job_payload(call.id, remote_payload, source="POLL")
    call.refresh_from_db()
    if call.status != "COMPLETED":
        return {
            "call_id": str(call.id),
            "remote_job_id": remote_job_id,
            "status": call.status,
            "credits": str(call.credits),
            "cost_equivalent": str(call.cost_equivalent) if call.cost_equivalent is not None else None,
            "billing_truth": str((call.requested_metadata or {}).get("billing_truth") or ""),
            "execution_recovered": False,
            "provider_replayed": False,
        }

    if str((call.requested_metadata or {}).get("billing_truth") or "") != "ACTUAL":
        raise GenXRecoveryError("completed GenX call has no authoritative actual usage; execution recovery is blocked")
    if call.cost_equivalent is None:
        raise GenXRecoveryError("completed GenX call has no authoritative monetary valuation; execution recovery is blocked")

    plan = WorkPlan.objects.select_related("job").filter(job_id=call.job_id).first()
    if plan is None:
        raise GenXRecoveryError("completed GenX call has no work plan to recover")
    try:
        spec = operation_spec(plan.operation)
    except WorkerRegistryError as exc:
        raise GenXRecoveryError(str(exc)) from exc
    if not spec.requires_genx:
        raise GenXRecoveryError("work plan operation is not provider-backed")
    if call.worker_id and call.worker.worker_class != spec.worker_class:
        raise GenXRecoveryError("completed GenX call worker does not match the work plan operation")

    execution = _execution_for_call(call)
    if Artifact.objects.filter(execution=execution, accepted=True).exists() and execution.status == "QA_PASSED":
        WorkPlan.objects.filter(pk=plan.pk).update(status=WorkPlan.Status.QA_PASSED, reason_codes=[], last_error_code="")
        return {
            "call_id": str(call.id),
            "remote_job_id": remote_job_id,
            "status": call.status,
            "credits": str(call.credits),
            "cost_equivalent": str(call.cost_equivalent),
            "billing_truth": "ACTUAL",
            "execution_id": execution.id,
            "execution_status": execution.status,
            "artifact_ids": list(Artifact.objects.filter(execution=execution).values_list("id", flat=True)),
            "execution_recovered": True,
            "already_recovered": True,
            "provider_replayed": False,
        }

    text, sources, recovery_evidence = _extract_completed_text(
        gateway=gateway,
        call=call,
        remote_payload=remote_payload,
        session_id=session_id,
        message_id=message_id,
        remote_job_id=remote_job_id,
    )
    if not text:
        raise GenXRecoveryError("completed GenX call contains no recoverable text result")

    metadata = dict(call.requested_metadata or {})
    metadata.update({
        "assistant_text": text,
        "recovered_execution": True,
        "recovered_without_provider_replay": True,
    })
    GenXCall.objects.filter(pk=call.pk).update(requested_metadata=metadata)
    call.requested_metadata = metadata

    if not execution.workspace:
        raise GenXRecoveryError("originating execution has no persisted workspace")
    workspace = Path(execution.workspace).resolve()
    job_root = Path(__import__("os").environ.get("AMARKTAI_JOB_ROOT", "/var/lib/amarktai-earn/jobs")).resolve()
    if not _inside(workspace, job_root):
        raise GenXRecoveryError("originating execution workspace is outside configured job storage")

    job, worker = _prepare_local_recovery(call=call, execution=execution, plan=plan)
    execution.refresh_from_db()
    request = WorkRequest(
        job_id=str(job.id),
        workspace=workspace,
        inputs={
            **dict(plan.input_spec or {}),
            "operation": plan.operation,
            "recovered_provider_text": text,
            "recovered_sources": sources,
            "recovered_model": call.model,
            "recovered_genx_call_id": str(call.id),
            "recovered_remote_job_id": remote_job_id,
        },
        worker_id=worker.id,
        execution_id=execution.id,
        attempt=execution.attempt,
    )
    result = spec.build().recover_completed_provider_result(request)
    if not result.ok:
        Execution.objects.filter(pk=execution.pk).update(
            status="FAILED",
            ended_at=timezone.now(),
            error_code="RECOVERY_MATERIALIZATION_FAILED",
            error_detail=(result.error or "completed provider result recovery failed")[:4000],
        )
        WorkPlan.objects.filter(pk=plan.pk).update(
            status=WorkPlan.Status.BLOCKED,
            reason_codes=["COMPLETED_PROVIDER_RESULT_MATERIALIZATION_FAILED"],
            last_error_code="RECOVERY_MATERIALIZATION_FAILED",
        )
        Worker.objects.filter(pk=worker.pk).update(status="ERROR", current_job=None, last_heartbeat=timezone.now())
        raise GenXRecoveryError(result.error or "completed provider result recovery failed")

    evidence = dict(result.evidence or {})
    evidence.update({
        "genx_call_id": str(call.id),
        "remote_job_id": remote_job_id,
        "provider_replayed": False,
        "recovery_session_history_present": bool(recovery_evidence.get("session_history")),
        "recovery_result_payload_present": bool(recovery_evidence.get("result_payload")),
    })
    result.evidence = evidence
    try:
        recovered_execution = finalize_successful_execution(
            job=job,
            execution=execution,
            worker=worker,
            operation=plan.operation,
            result=result,
            allow_repair=False,
            audit_actor="genx-recovery",
        )
    except ExecutionError as exc:
        WorkPlan.objects.filter(pk=plan.pk).update(
            status=WorkPlan.Status.BLOCKED,
            reason_codes=["COMPLETED_PROVIDER_RESULT_FINALIZATION_FAILED"],
            last_error_code=exc.__class__.__name__[:120],
        )
        raise GenXRecoveryError(str(exc)) from exc

    if recovered_execution.status == "QA_PASSED":
        WorkPlan.objects.filter(pk=plan.pk).update(
            status=WorkPlan.Status.QA_PASSED,
            reason_codes=[],
            last_error_code="",
        )
    else:
        WorkPlan.objects.filter(pk=plan.pk).update(
            status=WorkPlan.Status.NEEDS_REPAIR,
            reason_codes=["RECOVERED_RESULT_QA_FAILED"],
            last_error_code="",
        )

    AuditEvent.objects.create(
        event_type="genx.completed_execution_recovered",
        actor="genx-recovery",
        metadata={
            "call_id": str(call.id),
            "job_id": str(job.id),
            "plan_id": plan.id,
            "execution_id": recovered_execution.id,
            "execution_status": recovered_execution.status,
            "artifact_ids": list(Artifact.objects.filter(execution=recovered_execution).values_list("id", flat=True)),
            "credits": str(call.credits),
            "cost_equivalent": str(call.cost_equivalent),
            "provider_replayed": False,
        },
    )
    return {
        "call_id": str(call.id),
        "remote_job_id": remote_job_id,
        "status": call.status,
        "credits": str(call.credits),
        "cost_equivalent": str(call.cost_equivalent),
        "billing_truth": str((call.requested_metadata or {}).get("billing_truth") or ""),
        "execution_id": recovered_execution.id,
        "execution_status": recovered_execution.status,
        "plan_status": WorkPlan.objects.get(pk=plan.pk).status,
        "artifact_ids": list(Artifact.objects.filter(execution=recovered_execution).values_list("id", flat=True)),
        "execution_recovered": True,
        "already_recovered": False,
        "provider_replayed": False,
    }
