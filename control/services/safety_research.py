from __future__ import annotations

import hashlib
import json
import os
import re
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from control.models import (
    AuthorizedTarget, AuditEvent, BountyProgram, FindingReproduction, ProgramScopeVersion,
    SafetyBountySubmission, SafetyFinding, SafetyResearchAttempt, SanitizedEvaluationCase,
)
from control.services.admission import decide_admission
from control.services.execution import execute_registered_job


class SafetyAuthorizationError(RuntimeError):
    pass


SAFE_TEST_TYPES = {
    "PROMPT_INJECTION_EVALUATION", "AGENT_BEHAVIOR_EVALUATION",
    "POLICY_COMPLIANCE_EVALUATION", "ADVERSARIAL_ROBUSTNESS_EVALUATION",
}
PROHIBITED_PATTERNS = {
    "INTERNET_WIDE_SCANNING": r"\b(?:internet[- ]wide|random (?:ip|host|target)|mass scan)\b",
    "CREDENTIAL_ATTACK": r"\b(?:credential stuffing|password spraying|brute force (?:login|password))\b",
    "DDOS_OR_STRESS": r"\b(?:ddos|denial of service|stress test|flood target)\b",
    "PERSISTENCE": r"\b(?:install persistence|backdoor|maintain access)\b",
    "MALWARE": r"\b(?:malware|ransomware|keylogger|trojan)\b",
    "PHISHING": r"\b(?:phishing|steal credentials)\b",
    "DESTRUCTIVE_EXPLOITATION": r"\b(?:wipe data|destroy data|destructive exploit)\b",
    "SCOPE_BYPASS": r"\b(?:bypass scope|out[- ]of[- ]scope target)\b",
}


def _current_scope(program: BountyProgram) -> ProgramScopeVersion | None:
    now = timezone.now()
    return program.scope_versions.filter(
        active=True, effective_at__lte=now, expires_at__gt=now,
    ).order_by("-version").first()


def _authorization_reasons(*, program, scope, target, test_type, plan, estimated_spend, max_requests, job) -> list[str]:
    reasons = []
    if os.getenv("SAFETY_BOUNTY_EXECUTION_ENABLED", "0") != "1":
        reasons.append("SAFETY_BOUNTY_EXECUTION_DISABLED")
    if program.status != BountyProgram.Status.ACTIVE or not program.execution_enabled:
        reasons.append("BOUNTY_PROGRAM_NOT_ACTIVE")
    if not program.automation_allowed:
        reasons.append("BOUNTY_PROGRAM_AUTOMATION_NOT_ALLOWED")
    if scope is None:
        reasons.append("NO_SCOPE_NO_TESTING")
        return reasons
    if not scope.authorization_hash or len(scope.authorization_hash) != 64:
        reasons.append("SCOPE_AUTHORIZATION_HASH_INVALID")
    if scope.rate_limit_per_minute < 1:
        reasons.append("SCOPE_RATE_LIMIT_UNKNOWN")
    if scope.max_requests_per_attempt < 1:
        reasons.append("SCOPE_REQUEST_BOUND_UNKNOWN")
    if scope.max_spend_per_attempt <= 0:
        reasons.append("SCOPE_SPEND_BOUND_UNKNOWN")
    if target is None or not target.active or target.scope_version_id != scope.id:
        reasons.append("TARGET_NOT_IN_CURRENT_SCOPE")
    elif target.target_type == AuthorizedTarget.TargetType.REMOTE_PROGRAM:
        reasons.append("REMOTE_SAFETY_TARGET_EXECUTION_NOT_SUPPORTED_IN_V1")
    normalized_type = test_type.upper()
    allowed = {str(value).upper() for value in scope.allowed_test_types}
    prohibited = {str(value).upper() for value in scope.prohibited_test_types}
    if normalized_type not in SAFE_TEST_TYPES or normalized_type not in allowed or normalized_type in prohibited:
        reasons.append("SAFETY_TEST_TYPE_NOT_PERMITTED")
    if max_requests < 1 or max_requests > scope.max_requests_per_attempt:
        reasons.append("SAFETY_REQUEST_BOUND_EXCEEDED")
    cases = plan.get("test_cases") if isinstance(plan, dict) else None
    if not isinstance(cases, list) or not cases or len(cases) > max_requests:
        reasons.append("SAFETY_TEST_PLAN_INVALID_OR_UNBOUNDED")
    spend = Decimal(str(estimated_spend))
    configured_limit = Decimal(os.getenv("SAFETY_BOUNTY_MAX_SPEND_PER_ATTEMPT", "0"))
    if spend < 0 or spend > scope.max_spend_per_attempt or configured_limit <= 0 or spend > configured_limit:
        reasons.append("SAFETY_SPEND_NOT_BOUNDED")
    serialized = json.dumps(plan, sort_keys=True, default=str).casefold()
    reasons.extend(code for code, pattern in PROHIBITED_PATTERNS.items() if re.search(pattern, serialized, re.IGNORECASE))
    admission = decide_admission(
        purpose="SAFETY_RESEARCH", job=job, operation="ai_safety_evaluate", persist=True,
    )
    reasons.extend(admission.reason_codes)
    return list(dict.fromkeys(reasons))


@transaction.atomic
def authorize_safety_attempt(*, job, program: BountyProgram, canonical_target: str, test_type: str, plan: dict, estimated_spend=0, max_requests: int = 1) -> SafetyResearchAttempt:
    scope = _current_scope(program)
    if scope is None:
        raise SafetyAuthorizationError("NO_SCOPE_NO_TESTING")
    target = scope.authorized_targets.filter(canonical_target=canonical_target, active=True).first()
    if target is None:
        raise SafetyAuthorizationError("TARGET_NOT_IN_CURRENT_SCOPE")
    reasons = _authorization_reasons(
        program=program, scope=scope, target=target, test_type=test_type, plan=plan,
        estimated_spend=estimated_spend, max_requests=max_requests, job=job,
    )
    attempt = SafetyResearchAttempt.objects.create(
        job=job, program=program, scope_version=scope, target=target,
        test_type=test_type.upper(), status=SafetyResearchAttempt.Status.BLOCKED if reasons else SafetyResearchAttempt.Status.AUTHORIZED,
        plan=plan, authorization_snapshot={
            "authorization_hash": scope.authorization_hash, "scope_version": scope.version,
            "target": target.canonical_target, "target_type": target.target_type,
            "allowed_test_types": scope.allowed_test_types, "rate_limit_per_minute": scope.rate_limit_per_minute,
            "expires_at": scope.expires_at.isoformat(),
        },
        estimated_spend=estimated_spend, max_requests=max_requests, reason_codes=reasons,
    )
    AuditEvent.objects.create(
        severity="INFO" if not reasons else "WARNING",
        event_type="safety.attempt_authorized" if not reasons else "safety.attempt_blocked",
        actor="safety-authorization-gate",
        metadata={"attempt_id": attempt.id, "job_id": str(job.id), "reason_codes": reasons},
    )
    return attempt


def execute_safety_attempt(*, attempt_id: int, worker_id: str, workspace_root: str | None = None):
    blocked_reasons: list[str] = []
    with transaction.atomic():
        attempt = SafetyResearchAttempt.objects.select_for_update().select_related(
            "program", "scope_version", "target", "job",
        ).get(pk=attempt_id)
        if attempt.status != SafetyResearchAttempt.Status.AUTHORIZED:
            raise SafetyAuthorizationError("SAFETY_ATTEMPT_NOT_AUTHORIZED")
        now = timezone.now()
        current_scope = _current_scope(attempt.program)
        if current_scope is None or current_scope.id != attempt.scope_version_id:
            blocked_reasons.append("SAFETY_SCOPE_NOT_CURRENT")
        if attempt.authorization_snapshot.get("authorization_hash") != attempt.scope_version.authorization_hash:
            blocked_reasons.append("SCOPE_AUTHORIZATION_CHANGED")
        blocked_reasons.extend(_authorization_reasons(
            program=attempt.program, scope=attempt.scope_version, target=attempt.target,
            test_type=attempt.test_type, plan=attempt.plan, estimated_spend=attempt.estimated_spend,
            max_requests=attempt.max_requests, job=attempt.job,
        ))
        blocked_reasons = list(dict.fromkeys(blocked_reasons))
        if blocked_reasons:
            attempt.status = SafetyResearchAttempt.Status.BLOCKED
            attempt.reason_codes = blocked_reasons
            attempt.ended_at = now
            attempt.save(update_fields=["status", "reason_codes", "ended_at", "updated_at"])
            AuditEvent.objects.create(
                severity="WARNING", event_type="safety.attempt_blocked_at_execution",
                actor="safety-authorization-gate",
                metadata={"attempt_id": attempt.id, "job_id": str(attempt.job_id), "reason_codes": blocked_reasons},
            )
        else:
            attempt.status = SafetyResearchAttempt.Status.RUNNING
            attempt.started_at = now
            attempt.save(update_fields=["status", "started_at", "updated_at"])
    if blocked_reasons:
        raise SafetyAuthorizationError("SAFETY_ATTEMPT_REAUTHORIZATION_FAILED: " + ",".join(blocked_reasons))
    try:
        execution = execute_registered_job(
            job_id=attempt.job_id, worker_id=worker_id,
            inputs={"operation": "ai_safety_evaluate", "safety_attempt_id": attempt.id},
            workspace_root=workspace_root, expected_worker_class="ai_safety_research",
        )
    except Exception as exc:
        SafetyResearchAttempt.objects.filter(pk=attempt.id).update(
            status=SafetyResearchAttempt.Status.FAILED, ended_at=timezone.now(),
            result={"error_code": exc.__class__.__name__},
        )
        raise
    evidence = execution.result.get("worker_evidence") or {}
    with transaction.atomic():
        attempt = SafetyResearchAttempt.objects.select_for_update().get(pk=attempt.id)
        attempt.execution = execution
        attempt.executed_requests = int(evidence.get("requests_executed") or 0)
        attempt.status = SafetyResearchAttempt.Status.COMPLETED
        attempt.ended_at = timezone.now()
        attempt.result = {"qa": execution.result.get("qa") or {}, "candidate_findings": len(evidence.get("findings") or [])}
        attempt.save(update_fields=["execution", "executed_requests", "status", "ended_at", "result", "updated_at"])
        for row in evidence.get("findings") or []:
            SafetyFinding.objects.get_or_create(
                attempt=attempt, fingerprint=str(row["fingerprint"]),
                defaults={
                    "title": str(row["title"]), "impact": str(row["impact"]),
                    "severity": str(row["severity"]), "evidence": row.get("evidence") or {},
                },
            )
    return execution


@transaction.atomic
def record_finding_reproduction(*, finding: SafetyFinding, independent_reviewer: str, reproduced: bool, evidence: dict) -> FindingReproduction:
    reviewer = independent_reviewer.strip()
    primary = finding.attempt.execution.worker_id if finding.attempt.execution_id else ""
    if not reviewer or reviewer == primary:
        raise SafetyAuthorizationError("INDEPENDENT_REVIEWER_REQUIRED")
    if not isinstance(evidence, dict) or not evidence:
        raise SafetyAuthorizationError("REPRODUCTION_EVIDENCE_REQUIRED")
    evidence_hash = hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest()
    reproduction = FindingReproduction.objects.create(
        finding=finding, independent_reviewer=reviewer, reproduced=reproduced,
        evidence=evidence, evidence_hash=evidence_hash,
    )
    finding.status = SafetyFinding.Status.REPRODUCED if reproduced else SafetyFinding.Status.NOT_REPRODUCED
    finding.save(update_fields=["status", "updated_at"])
    return reproduction


def mark_duplicate_check(*, finding: SafetyFinding, duplicates_found: bool, evidence: dict) -> SafetyFinding:
    if not isinstance(evidence, dict) or not evidence:
        raise SafetyAuthorizationError("DUPLICATE_CHECK_EVIDENCE_REQUIRED")
    finding.duplicate_checked = True
    finding.evidence = {**finding.evidence, "duplicate_check": evidence}
    if duplicates_found:
        finding.status = SafetyFinding.Status.REJECTED
    elif finding.status == SafetyFinding.Status.REPRODUCED:
        finding.status = SafetyFinding.Status.SUBMISSION_READY
    finding.save(update_fields=["duplicate_checked", "evidence", "status", "updated_at"])
    return finding


@transaction.atomic
def prepare_bounty_submission(*, finding: SafetyFinding, submission_method: str) -> SafetyBountySubmission:
    finding.refresh_from_db()
    if finding.status != SafetyFinding.Status.SUBMISSION_READY or not finding.duplicate_checked:
        raise SafetyAuthorizationError("REPRODUCTION_AND_DUPLICATE_CHECK_REQUIRED")
    if finding.contains_private_data:
        raise SafetyAuthorizationError("PRIVATE_TARGET_DATA_MUST_NOT_BE_SUBMITTED")
    if _current_scope(finding.attempt.program) is None:
        raise SafetyAuthorizationError("SAFETY_SCOPE_NOT_CURRENT")
    artifact = finding.attempt.execution.artifacts.order_by("id").first() if finding.attempt.execution_id else None
    submission, _ = SafetyBountySubmission.objects.get_or_create(
        finding=finding,
        defaults={"submission_method": submission_method[:120], "report_artifact": artifact},
    )
    return submission


@transaction.atomic
def create_sanitized_evaluation_case(*, finding: SafetyFinding, case_type: str, prompt: str, expected_behavior: str, rights_confirmed: bool) -> SanitizedEvaluationCase:
    if finding.status not in {SafetyFinding.Status.REPRODUCED, SafetyFinding.Status.SUBMISSION_READY, SafetyFinding.Status.SUBMITTED}:
        raise SafetyAuthorizationError("ONLY_REPRODUCED_FINDINGS_CAN_BECOME_EVALUATION_CASES")
    if not rights_confirmed:
        raise SafetyAuthorizationError("EVALUATION_CASE_RIGHTS_REQUIRED")
    sanitized_prompt = prompt.strip()
    combined = f"{sanitized_prompt}\n{expected_behavior}"
    if not sanitized_prompt or len(sanitized_prompt) > 2000 or "```" in combined or "http://" in combined or "https://" in combined:
        raise SafetyAuthorizationError("EVALUATION_CASE_NOT_SAFELY_SANITIZED")
    if finding.attempt.target.canonical_target.casefold() in combined.casefold():
        raise SafetyAuthorizationError("PRIVATE_TARGET_IDENTIFIER_NOT_REMOVED")
    if any(re.search(pattern, combined, re.IGNORECASE) for pattern in PROHIBITED_PATTERNS.values()):
        raise SafetyAuthorizationError("HARMFUL_TEST_DETAIL_NOT_REMOVED")
    case = SanitizedEvaluationCase.objects.create(
        finding=finding, case_type=case_type[:80], prompt=sanitized_prompt,
        expected_behavior=expected_behavior.strip(),
        provenance={"finding_fingerprint": finding.fingerprint, "program": finding.attempt.program.name},
        rights_confirmed=True, private_data_removed=True, harmful_detail_removed=True,
    )
    finding.sanitized = True
    finding.save(update_fields=["sanitized", "updated_at"])
    return case
