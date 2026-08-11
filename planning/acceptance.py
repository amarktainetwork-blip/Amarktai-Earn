from __future__ import annotations

import hashlib
import json
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Max

from control.models import Artifact, AuditEvent, Execution, Job, QAResult
from planning.models import AcceptanceContract, AcceptanceEvaluation, WorkPlan

COMPILER_VERSION = "acceptance-compiler-v1"
EVALUATOR_VERSION = "acceptance-evaluator-v1"
CODING_WORKERS = {"code_small", "code_heavy", "ci_testing"}


class AcceptanceGateError(RuntimeError):
    def __init__(self, reason_codes: list[str]):
        self.reason_codes = list(dict.fromkeys(reason_codes))
        super().__init__(", ".join(self.reason_codes) or "acceptance gate blocked submission")


def _payload(job: Job) -> dict[str, Any]:
    return job.normalized_payload if isinstance(job.normalized_payload, dict) else {}


def _json_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _explicit_acceptance(payload: dict[str, Any]) -> list[str]:
    value = payload.get("acceptance_criteria", payload.get("acceptanceCriteria"))
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [f"{key}: {value[key]}" for key in sorted(value) if value[key] not in (None, "", [], {})]
    return []


def source_material(job: Job, plan: WorkPlan | None = None) -> dict[str, Any]:
    payload = _payload(job)
    requirements = {
        key: payload[key]
        for key in (
            "description", "requirements", "instructions", "task", "deliverables",
            "acceptance_criteria", "acceptanceCriteria", "test_command", "testCommand",
            "ci_command", "ciCommand", "verification_command", "verificationCommand",
        )
        if payload.get(key) not in (None, "", [], {})
    }
    try:
        manifest = job.asset_manifest
        asset_manifest = {
            "version": manifest.version,
            "status": manifest.status,
            "sha256": manifest.manifest_sha256,
            "roles": manifest.roles,
        }
    except ObjectDoesNotExist:
        asset_manifest = None
    try:
        repository = job.repository_snapshot
        repository_snapshot = {
            "status": repository.status,
            "url": repository.repository_url,
            "ref": repository.ref,
            "commit": repository.commit_sha,
        }
    except ObjectDoesNotExist:
        repository_snapshot = None
    return {
        "job": {"title": job.title, "task_class": job.task_class, "requirements": requirements},
        "plan": None if plan is None else {
            "planner_version": plan.planner_version,
            "worker_class": plan.worker_class,
            "operation": plan.operation,
            "input_spec": plan.input_spec,
        },
        "asset_manifest": asset_manifest,
        "repository_snapshot": repository_snapshot,
    }


def source_hash(job: Job, plan: WorkPlan | None = None) -> str:
    return _json_hash(source_material(job, plan))


def _criteria(job: Job, plan: WorkPlan | None) -> list[dict[str, Any]]:
    rows = [
        {"id": "artifact-present", "label": "A persisted deliverable artifact exists", "critical": True, "verification": "deterministic"},
        {"id": "deterministic-qa", "label": "The registered deterministic QA profile passes", "critical": True, "verification": "deterministic"},
    ]
    if plan and plan.worker_class in CODING_WORKERS:
        rows.append({"id": "coding-tests", "label": "The explicit repository test command passes", "critical": True, "verification": "deterministic"})
    for index, label in enumerate(_explicit_acceptance(_payload(job)), start=1):
        rows.append({"id": f"source-criterion-{index}", "label": label[:1000], "critical": True, "verification": "semantic"})
    return rows


@transaction.atomic
def compile_acceptance_contract(job: Job, plan: WorkPlan | None = None) -> AcceptanceContract:
    if plan is None:
        plan = WorkPlan.objects.filter(job=job).first()
    material = source_material(job, plan)
    fingerprint = _json_hash(material)
    current = AcceptanceContract.objects.select_for_update().filter(job=job, is_current=True).first()
    if current and current.source_hash == fingerprint and current.compiler_version == COMPILER_VERSION:
        return current
    if current:
        current.is_current = False
        current.status = AcceptanceContract.Status.STALE
        current.reason_codes = list(dict.fromkeys([*current.reason_codes, "SOURCE_INPUT_CHANGED"]))
        current.save(update_fields=["is_current", "status", "reason_codes", "updated_at"])
    version = (AcceptanceContract.objects.filter(job=job).aggregate(value=Max("version"))["value"] or 0) + 1
    criteria = _criteria(job, plan)
    contract = AcceptanceContract.objects.create(
        job=job,
        version=version,
        status=AcceptanceContract.Status.ACTIVE,
        source_hash=fingerprint,
        compiler_version=COMPILER_VERSION,
        source_requirements=material,
        compiled_task={
            "objective": job.title,
            "task_class": job.task_class,
            "operation": plan.operation if plan else "",
            "worker_class": plan.worker_class if plan else "",
            "criterion_ids": [row["id"] for row in criteria],
            "grounding_hash": fingerprint,
        },
        criteria=criteria,
    )
    AuditEvent.objects.create(
        event_type="job.acceptance_contract_compiled",
        actor="acceptance-compiler",
        metadata={"job_id": str(job.id), "contract_id": contract.id, "version": version, "source_hash": fingerprint},
    )
    return contract


def acceptance_execution_payload(contract: AcceptanceContract) -> dict[str, Any]:
    """Structured prompt context supplied to workers without inventing requirements."""
    return {
        "contract_id": contract.id,
        "version": contract.version,
        "compiler_version": contract.compiler_version,
        "source_hash": contract.source_hash,
        "task": contract.compiled_task,
        "criteria": contract.criteria,
    }


def _semantic_result(execution: Execution, semantic_criteria: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[str]]:
    if not semantic_criteria:
        return AcceptanceEvaluation.SemanticState.PASS, [], []
    result = execution.result if isinstance(execution.result, dict) else {}
    worker = result.get("worker_evidence") if isinstance(result.get("worker_evidence"), dict) else {}
    evidence = worker.get("semantic_acceptance", result.get("semantic_acceptance"))
    if not isinstance(evidence, dict):
        return AcceptanceEvaluation.SemanticState.UNCERTAIN, [
            {"id": row["id"], "status": "UNCERTAIN", "evidence": "No structured semantic evidence"}
            for row in semantic_criteria
        ], ["SEMANTIC_EVIDENCE_MISSING"]
    raw_state = str(evidence.get("status") or "UNCERTAIN").upper()
    state = raw_state if raw_state in {"PASS", "FAIL", "UNCERTAIN"} else "UNCERTAIN"
    raw_results = evidence.get("criteria")
    mapping: dict[str, str] = {}
    if isinstance(raw_results, dict):
        mapping = {str(key): str(value).upper() for key, value in raw_results.items()}
    elif isinstance(raw_results, list):
        mapping = {
            str(item.get("id")): str(item.get("status") or "UNCERTAIN").upper()
            for item in raw_results if isinstance(item, dict) and item.get("id")
        }
    rows = []
    failures = []
    for criterion in semantic_criteria:
        criterion_state = mapping.get(criterion["id"], state)
        if criterion_state not in {"PASS", "FAIL", "UNCERTAIN"}:
            criterion_state = "UNCERTAIN"
        rows.append({"id": criterion["id"], "status": criterion_state, "evidence": "structured semantic judgment"})
        if criterion_state != "PASS" and criterion.get("critical", True):
            failures.append(f"SEMANTIC_{criterion_state}:{criterion['id']}")
    if failures and state == "PASS":
        state = "FAIL" if any("SEMANTIC_FAIL" in item for item in failures) else "UNCERTAIN"
    return state, rows, failures


@transaction.atomic
def evaluate_execution_acceptance(execution_id: int) -> AcceptanceEvaluation:
    execution = Execution.objects.select_for_update().select_related("job").get(pk=execution_id)
    plan = WorkPlan.objects.filter(job=execution.job).first()
    contract = compile_acceptance_contract(execution.job, plan)
    failures: list[str] = []
    rows: list[dict[str, Any]] = []
    qa = QAResult.objects.filter(job=execution.job, execution=execution).order_by("-created_at").first()
    artifact_exists = Artifact.objects.filter(job=execution.job, execution=execution).exists()
    rows.append({"id": "artifact-present", "status": "PASS" if artifact_exists else "FAIL", "evidence": "persisted artifact record"})
    if not artifact_exists:
        failures.append("ARTIFACT_MISSING")
    qa_passed = bool(qa and qa.passed)
    rows.append({"id": "deterministic-qa", "status": "PASS" if qa_passed else "FAIL", "evidence": qa.check_type if qa else "no QA result"})
    if not qa_passed:
        failures.append("DETERMINISTIC_QA_FAILED")
    if plan and plan.worker_class in CODING_WORKERS:
        test_command = str((plan.input_spec or {}).get("test_command") or "").strip()
        result = execution.result if isinstance(execution.result, dict) else {}
        worker = result.get("worker_evidence") if isinstance(result.get("worker_evidence"), dict) else {}
        exit_code = worker.get("test_exit_code")
        coding_passed = bool(test_command and exit_code is not None and int(exit_code) == 0)
        rows.append({"id": "coding-tests", "status": "PASS" if coding_passed else "FAIL", "evidence": {"command_present": bool(test_command), "exit_code": exit_code}})
        if not coding_passed:
            failures.append("CODING_TEST_EVIDENCE_FAILED")
    semantic_criteria = [row for row in contract.criteria if row.get("verification") == "semantic"]
    semantic_state, semantic_rows, semantic_failures = _semantic_result(execution, semantic_criteria)
    rows.extend(semantic_rows)
    failures.extend(semantic_failures)
    deterministic_passed = not any(
        item in failures for item in ("ARTIFACT_MISSING", "DETERMINISTIC_QA_FAILED", "CODING_TEST_EVIDENCE_FAILED")
    )
    current_hash = source_hash(execution.job, plan)
    if contract.source_hash != current_hash or not contract.is_current:
        failures.append("ACCEPTANCE_CONTRACT_STALE")
    ready = deterministic_passed and semantic_state == AcceptanceEvaluation.SemanticState.PASS and not failures
    evaluation, _ = AcceptanceEvaluation.objects.update_or_create(
        execution=execution,
        defaults={
            "contract": contract,
            "evaluator_version": EVALUATOR_VERSION,
            "deterministic_passed": deterministic_passed,
            "semantic_state": semantic_state,
            "submission_ready": ready,
            "criterion_results": rows,
            "critical_failures": list(dict.fromkeys(failures)),
            "evidence": {"qa_result_id": qa.id if qa else None, "source_hash": current_hash},
        },
    )
    AuditEvent.objects.create(
        severity="INFO" if ready else "WARNING",
        event_type="job.acceptance_passed" if ready else "job.acceptance_blocked",
        actor="acceptance-evaluator",
        metadata={"job_id": str(execution.job_id), "execution_id": execution.id, "contract_id": contract.id, "semantic_state": semantic_state, "reason_codes": evaluation.critical_failures},
    )
    return evaluation


def require_submission_ready(execution: Execution) -> AcceptanceEvaluation:
    plan = WorkPlan.objects.filter(job=execution.job).first()
    current = AcceptanceContract.objects.filter(job=execution.job, is_current=True).first()
    if current is None:
        current = compile_acceptance_contract(execution.job, plan)
    if current.source_hash != source_hash(execution.job, plan):
        compile_acceptance_contract(execution.job, plan)
        raise AcceptanceGateError(["ACCEPTANCE_CONTRACT_STALE"])
    evaluation = AcceptanceEvaluation.objects.filter(execution=execution, contract=current).first()
    if evaluation is None:
        evaluation = evaluate_execution_acceptance(execution.id)
    if not evaluation.submission_ready:
        raise AcceptanceGateError(evaluation.critical_failures or [f"SEMANTIC_{evaluation.semantic_state}"])
    return evaluation
