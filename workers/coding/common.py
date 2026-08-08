from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from control.models import Job
from control.services.admission import AdmissionDenied, require_admission
from control.sandbox_tokens import issue_sandbox_token
from sandbox_broker.client import SandboxBrokerClient, SandboxBrokerError
from workers.genx_support import GenXWorkerError, select_specialist


class CodingWorkerError(RuntimeError):
    pass


def configured_broker() -> SandboxBrokerClient:
    return SandboxBrokerClient()


def _repo_relative(path: str) -> str:
    root = Path(os.getenv("AMARKTAI_REPO_ROOT", "/var/lib/amarktai-earn/repos")).resolve()
    candidate = Path(path).resolve()
    if root not in candidate.parents:
        raise CodingWorkerError("repository snapshot is outside configured repository root")
    relative = candidate.relative_to(root).as_posix()
    parts = Path(relative).parts
    if len(parts) != 2 or len(parts[1]) != 40:
        raise CodingWorkerError("repository snapshot path does not match job/commit layout")
    return relative


def _job_budget(job_id) -> Decimal:
    try:
        job = Job.objects.select_related("jobscore").get(pk=job_id)
        budget = Decimal(job.jobscore.max_genx_credits)
    except Exception as exc:
        raise CodingWorkerError("job has no persisted GenX credit budget") from exc
    if budget <= 0:
        raise CodingWorkerError("coding job has no positive GenX credit budget")
    return budget


def run_ai_coding_sandbox(request, *, agent: str) -> dict[str, Any]:
    if os.getenv("SANDBOX_CODING_ENABLED", "0") != "1":
        raise CodingWorkerError("coding sandboxes are disabled")
    source = str(request.inputs.get("repository_path") or "")
    task = str(request.inputs.get("instructions") or "").strip()
    test_command = str(request.inputs.get("test_command") or "").strip()
    if not source or not task or not test_command:
        raise CodingWorkerError("coding request is missing repository, instructions, or test command")
    try:
        selected = select_specialist("code", "coding", "software", fallback_category="text")
    except GenXWorkerError as exc:
        raise CodingWorkerError(str(exc)) from exc
    budget = _job_budget(request.job_id)
    token = issue_sandbox_token(
        job_id=request.job_id,
        worker_id=request.worker_id,
        model=selected.model_id,
        max_credits=budget,
        ttl_seconds=int(os.getenv("SANDBOX_TOKEN_TTL_SECONDS", "1800")),
    )
    try:
        require_admission(purpose="SANDBOX", job=Job.objects.get(pk=request.job_id), operation=str(request.inputs.get("operation") or ""))
        return configured_broker().run({
            "agent": agent,
            "snapshot_rel": _repo_relative(source),
            "task": task,
            "test_command": test_command,
            "model": selected.model_id,
            "scoped_token": token,
            "job_id": request.job_id,
            "worker_id": request.worker_id,
            "execution_id": request.execution_id,
            "attempt": request.attempt,
        })
    except (SandboxBrokerError, AdmissionDenied) as exc:
        raise CodingWorkerError(str(exc)) from exc


def run_ci_sandbox(request) -> dict[str, Any]:
    if os.getenv("SANDBOX_CODING_ENABLED", "0") != "1":
        raise CodingWorkerError("coding sandboxes are disabled")
    source = str(request.inputs.get("repository_path") or "")
    test_command = str(request.inputs.get("test_command") or "").strip()
    if not source or not test_command:
        raise CodingWorkerError("CI request is missing repository or explicit test command")
    try:
        require_admission(purpose="SANDBOX", job=Job.objects.get(pk=request.job_id), operation=str(request.inputs.get("operation") or ""))
        return configured_broker().run({
            "agent": "ci",
            "snapshot_rel": _repo_relative(source),
            "task": str(request.inputs.get("instructions") or "Run the configured test command."),
            "test_command": test_command,
            "job_id": request.job_id,
            "worker_id": request.worker_id,
            "execution_id": request.execution_id,
            "attempt": request.attempt,
        })
    except (SandboxBrokerError, AdmissionDenied) as exc:
        raise CodingWorkerError(str(exc)) from exc
