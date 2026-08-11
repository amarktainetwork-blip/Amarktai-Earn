from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from control.models import Job
from control.services.admission import AdmissionDenied, require_admission
from control.services.dependencies import DependencyPreparationError, DependencyRequest, prepare_dependencies
from control.sandbox_tokens import issue_sandbox_token
from sandbox_broker.client import SandboxBrokerClient, SandboxBrokerError
from planning.models import RepositorySnapshot
from gateways.genx.service import GenXGateway
from workers.genx_support import GenXWorkerError, capability_model_ids


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


def _dependency_cache(request, broker: SandboxBrokerClient) -> str:
    raw = request.inputs.get("dependency_request")
    if not isinstance(raw, dict):
        return ""
    try:
        dependency = DependencyRequest(
            ecosystem=str(raw["ecosystem"]), manifest_path=str(raw["manifest_path"]), manifest_hash=str(raw["manifest_hash"]),
        )
        snapshot = RepositorySnapshot.objects.get(pk=request.inputs["repository_snapshot_id"], job_id=request.job_id)
        _row, cache_key = prepare_dependencies(job=Job.objects.get(pk=request.job_id), snapshot=snapshot, request=dependency, broker=broker)
        return cache_key
    except (KeyError, RepositorySnapshot.DoesNotExist, DependencyPreparationError) as exc:
        raise CodingWorkerError("safe dependency preparation failed") from exc


def _dependency_storage_estimate(request) -> int:
    if not isinstance(request.inputs.get("dependency_request"), dict):
        return 0
    try:
        return max(1024 * 1024, int(os.getenv("DEPENDENCY_MAX_CACHE_BYTES", "536870912")))
    except ValueError:
        return 536870912


def run_ai_coding_sandbox(request, *, agent: str) -> dict[str, Any]:
    if os.getenv("SANDBOX_CODING_ENABLED", "0") != "1":
        raise CodingWorkerError("coding sandboxes are disabled")
    source = str(request.inputs.get("repository_path") or "")
    task = str(request.inputs.get("instructions") or "").strip()
    test_command = str(request.inputs.get("test_command") or "").strip()
    if not source or not task or not test_command:
        raise CodingWorkerError("coding request is missing repository, instructions, or test command")
    try:
        eligible = capability_model_ids("code", "coding", "software", fallback_category="text")
    except GenXWorkerError as exc:
        raise CodingWorkerError(str(exc)) from exc
    budget = _job_budget(request.job_id)
    job = Job.objects.get(pk=request.job_id)
    estimated = min(
        Decimal(str(request.inputs.get("estimated_genx_credits") or os.getenv("GENX_DEFAULT_ESTIMATED_CREDITS", "0.25"))),
        budget,
    )
    selected = GenXGateway().select_model(
        task_class="coding_sandbox",
        category="text",
        eligible_model_ids=eligible,
        required_quality=Decimal(str(request.inputs.get("minimum_quality", "0.85"))),
        expected_revenue=job.reward,
        max_genx_credits=budget,
        estimated_credits=estimated,
        accounting_currency=job.currency,
        allow_exploration=bool(request.inputs.get("allow_model_exploration", False)),
        economically_fragile=bool(request.inputs.get("economically_fragile", False)),
    )
    token = issue_sandbox_token(
        job_id=request.job_id,
        worker_id=request.worker_id,
        model=selected.model_id,
        max_credits=budget,
        ttl_seconds=int(os.getenv("SANDBOX_TOKEN_TTL_SECONDS", "1800")),
    )
    try:
        require_admission(purpose="SANDBOX", job=Job.objects.get(pk=request.job_id), operation=str(request.inputs.get("operation") or ""), expected_storage_bytes=_dependency_storage_estimate(request))
        broker = configured_broker()
        dependency_cache_key = _dependency_cache(request, broker)
        return broker.run({
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
            "dependency_cache_key": dependency_cache_key,
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
        require_admission(purpose="SANDBOX", job=Job.objects.get(pk=request.job_id), operation=str(request.inputs.get("operation") or ""), expected_storage_bytes=_dependency_storage_estimate(request))
        broker = configured_broker()
        dependency_cache_key = _dependency_cache(request, broker)
        return broker.run({
            "agent": "ci",
            "snapshot_rel": _repo_relative(source),
            "task": str(request.inputs.get("instructions") or "Run the configured test command."),
            "test_command": test_command,
            "job_id": request.job_id,
            "worker_id": request.worker_id,
            "execution_id": request.execution_id,
            "attempt": request.attempt,
            "dependency_cache_key": dependency_cache_key,
        })
    except (SandboxBrokerError, AdmissionDenied) as exc:
        raise CodingWorkerError(str(exc)) from exc
