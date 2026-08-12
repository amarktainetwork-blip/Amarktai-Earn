from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import Artifact, AuditEvent, Execution, Job
from workers.registry import registered_operations


BRANCH_RE = re.compile(r"^self-improve/[a-z0-9][a-z0-9._/-]{2,100}$")
FORBIDDEN_BRANCHES = {"main", "master", "production", "prod"}


class SelfImprovementGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class SelfImprovementReviewManifest:
    schema_version: int
    job_id: str
    branch_name: str
    gap_summary: str
    source_refs: tuple[str, ...]
    oss_candidates: tuple[dict[str, Any], ...]
    code_execution_id: int
    test_execution_id: int
    patch_artifact_ids: tuple[int, ...]
    test_artifact_ids: tuple[int, ...]
    required_owner_actions: tuple[str, ...]
    merge_allowed: bool
    deploy_allowed: bool

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def self_improvement_contract() -> dict[str, Any]:
    operations = set(registered_operations())
    required = {"code_change_heavy", "run_repository_tests", "defensive_code_review"}
    missing = sorted(required - operations)
    return {
        "status": "PASS" if not missing else "FAIL",
        "required_operations": sorted(required),
        "missing_operations": missing,
        "workflow": [
            "detect_gap",
            "record_source_evidence",
            "record_approved_oss_candidates",
            "isolated_code_change",
            "independent_repository_tests",
            "defensive_code_review",
            "create_review_manifest",
            "owner_or_authorized_github_pr_publication",
            "owner_approval",
            "separate_deploy_gate",
        ],
        "production_self_merge": False,
        "production_self_deploy": False,
    }


def _execution(job: Job, execution_id: int, *, allowed_workers: set[str]) -> Execution:
    execution = Execution.objects.select_related("worker").filter(pk=execution_id, job=job).first()
    if execution is None:
        raise SelfImprovementGateError("referenced execution does not belong to the improvement job")
    if execution.status != "QA_PASSED":
        raise SelfImprovementGateError("self-improvement evidence requires QA_PASSED executions")
    worker_class = execution.worker.worker_class if execution.worker_id else ""
    if worker_class not in allowed_workers:
        raise SelfImprovementGateError(f"unexpected worker class for self-improvement evidence: {worker_class or 'NONE'}")
    return execution


def _accepted_artifacts(execution: Execution) -> list[Artifact]:
    rows = list(Artifact.objects.filter(execution=execution, accepted=True).order_by("id"))
    if not rows:
        raise SelfImprovementGateError("self-improvement evidence requires accepted artifacts")
    for artifact in rows:
        path = Path(artifact.path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise SelfImprovementGateError("accepted self-improvement artifact is missing or empty")
    return rows


def create_self_improvement_review_manifest(
    *,
    job_id,
    branch_name: str,
    gap_summary: str,
    source_refs: list[str],
    oss_candidates: list[dict[str, Any]],
    code_execution_id: int,
    test_execution_id: int,
) -> dict[str, Any]:
    """Create an auditable review-ready manifest; never publish, merge, or deploy code.

    The caller must already have produced a QA-passed isolated coding execution and
    an independent QA-passed test/review execution.  This function performs no
    network mutation and deliberately stops before GitHub publication.
    """
    contract = self_improvement_contract()
    if contract["status"] != "PASS":
        raise SelfImprovementGateError("self-improvement operation contract is incomplete")
    branch = str(branch_name or "").strip().casefold()
    if branch in FORBIDDEN_BRANCHES or not BRANCH_RE.fullmatch(branch):
        raise SelfImprovementGateError("self-improvement branch must be an isolated self-improve/* branch")
    summary = " ".join(str(gap_summary or "").split())
    if len(summary) < 20 or len(summary) > 4000:
        raise SelfImprovementGateError("gap summary must contain bounded substantive evidence")
    clean_refs = tuple(dict.fromkeys(str(value).strip() for value in source_refs if str(value).strip()))
    if not clean_refs:
        raise SelfImprovementGateError("self-improvement requires source evidence")
    if len(clean_refs) > 50:
        raise SelfImprovementGateError("too many source references")
    clean_oss: list[dict[str, Any]] = []
    for row in oss_candidates:
        if not isinstance(row, dict):
            raise SelfImprovementGateError("OSS candidate evidence must be structured")
        name = " ".join(str(row.get("name") or "").split())
        url = str(row.get("url") or "").strip()
        license_name = " ".join(str(row.get("license") or "").split())
        approved = row.get("approved") is True
        if not name or not url.startswith("https://") or not license_name:
            raise SelfImprovementGateError("OSS candidates require name, public HTTPS source, and license evidence")
        clean_oss.append({"name": name[:200], "url": url[:2000], "license": license_name[:120], "approved": approved})
    if len(clean_oss) > 20:
        raise SelfImprovementGateError("too many OSS candidates")

    with transaction.atomic():
        job = Job.objects.select_for_update().get(pk=job_id)
        code_execution = _execution(job, code_execution_id, allowed_workers={"code_heavy", "code_small"})
        test_execution = _execution(job, test_execution_id, allowed_workers={"ci_testing", "defensive_code_review"})
        if test_execution.id == code_execution.id:
            raise SelfImprovementGateError("independent test/review execution must differ from coding execution")
        code_artifacts = _accepted_artifacts(code_execution)
        test_artifacts = _accepted_artifacts(test_execution)
        manifest = SelfImprovementReviewManifest(
            schema_version=1,
            job_id=str(job.id),
            branch_name=branch,
            gap_summary=summary,
            source_refs=clean_refs,
            oss_candidates=tuple(clean_oss),
            code_execution_id=code_execution.id,
            test_execution_id=test_execution.id,
            patch_artifact_ids=tuple(row.id for row in code_artifacts),
            test_artifact_ids=tuple(row.id for row in test_artifacts),
            required_owner_actions=(
                "Publish the reviewed patch as a pull request through an authorized GitHub boundary.",
                "Review CI and approve the pull request explicitly.",
                "Run the separate production deployment gate after merge.",
            ),
            merge_allowed=False,
            deploy_allowed=False,
        )
        payload = manifest.payload()
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        existing = AuditEvent.objects.filter(
            event_type="self_improvement.review_ready",
            metadata__job_id=str(job.id),
            metadata__fingerprint=fingerprint,
        ).first()
        if existing is None:
            AuditEvent.objects.create(
                event_type="self_improvement.review_ready",
                actor="self-improvement-gate",
                metadata={
                    "job_id": str(job.id),
                    "fingerprint": fingerprint,
                    "manifest": payload,
                    "created_at": timezone.now().isoformat(),
                    "github_mutation_performed": False,
                    "merge_performed": False,
                    "deploy_performed": False,
                },
            )
        return {"fingerprint": fingerprint, "manifest": payload, "already_recorded": existing is not None}
