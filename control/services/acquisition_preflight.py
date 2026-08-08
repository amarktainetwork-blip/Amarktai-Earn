from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from control.models import AcquisitionPreflight, AuditEvent, GenXAccountSnapshot, MarketPolicyVersion
from control.services.admission import decide_admission
from control.services.autonomy import acquisition_autonomy
from control.services.workload_policy import evaluate_job
from control.services.profit_brain import capture_capacity, evaluate_opportunity, persist_opportunity_decision
from workers.registry import WorkerRegistryError, operation_spec
from django.utils import timezone


CENT = Decimal("0.01")


def _decimal_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except Exception:
        return Decimal(default)


def _payload_strings(job) -> list[str]:
    payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    values: list[str] = []
    for key in ("description", "requirements", "instructions", "task", "deliverables", "filename", "source_filename", "input_file"):
        value = payload.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(item) for item in value)
    return values


def infer_operation(job) -> tuple[str, Decimal, list[str]]:
    payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    explicit = str(payload.get("operation") or "").strip()
    if explicit:
        try:
            operation_spec(explicit)
            return explicit, Decimal("0.98"), []
        except WorkerRegistryError:
            return "", Decimal("0"), ["EXPLICIT_OPERATION_NOT_REGISTERED"]
    text = " ".join([job.title, *_payload_strings(job)]).casefold()
    if "json" in text and "csv" in text and any(term in text for term in ("convert", "conversion", "to csv")):
        return "json_to_csv", Decimal("0.90"), []
    if "csv" in text and any(term in text for term in ("normalize", "normalise", "clean", "standardize", "standardise")):
        return "csv_normalize", Decimal("0.88"), []
    return "", Decimal("0"), ["OPERATION_NOT_UNAMBIGUOUS"]


def _input_suffixes(job) -> set[str]:
    payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    found: set[str] = set()
    for key in ("filename", "source_filename", "input_file", "attachments", "files"):
        value = payload.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                item = item.get("name") or item.get("filename") or item.get("url")
            if isinstance(item, str):
                suffix = Path(item.split("?", 1)[0]).suffix.casefold()
                if suffix:
                    found.add(suffix)
    return found


def _expected_storage_bytes(job) -> int:
    payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    candidates = [payload.get(key) for key in ("source_size_bytes", "input_size_bytes", "attachment_size_bytes")]
    attachments = payload.get("attachments")
    if isinstance(attachments, list):
        candidates.extend(item.get("size_bytes") for item in attachments if isinstance(item, dict))
    total = 0
    for candidate in candidates:
        try:
            total += max(0, int(candidate or 0))
        except (TypeError, ValueError):
            continue
    multiplier = max(1, int(os.getenv("ACQUISITION_STORAGE_MULTIPLIER", "3")))
    return total * multiplier


def run_acquisition_preflight(job, *, persist: bool = True):
    reasons: list[str] = []
    operation, inference_confidence, infer_reasons = infer_operation(job)
    reasons.extend(infer_reasons)
    spec = None
    if operation:
        try:
            spec = operation_spec(operation)
        except WorkerRegistryError:
            reasons.append("WORKER_OPERATION_NOT_REGISTERED")

    policy = evaluate_job(job)
    reasons.extend(policy.reason_codes)
    switch_enabled = os.getenv("AGENTGIGS_AUTO_APPLY_ENABLED", "0") == "1"
    autonomy = acquisition_autonomy(switch_enabled=switch_enabled)
    reasons.extend(autonomy.reason_codes)

    enabled_operations = {item.strip() for item in os.getenv("ACQUISITION_ENABLED_OPERATIONS", "json_to_csv,csv_normalize").split(",") if item.strip()}
    if not operation or operation not in enabled_operations:
        reasons.append("OPERATION_NOT_ENABLED_FOR_ACQUISITION")
    if spec and not spec.qa_profile:
        reasons.append("QA_PROFILE_NOT_REGISTERED")
    if spec:
        try:
            spec.build()
        except Exception:
            reasons.append("WORKER_RUNTIME_UNAVAILABLE")
        suffixes = _input_suffixes(job)
        if spec.input_suffixes and not suffixes:
            reasons.append("INPUT_TYPE_NOT_DECLARED")
        elif spec.input_suffixes and not suffixes.intersection(spec.input_suffixes):
            reasons.append("INPUT_TYPE_NOT_SUPPORTED")
        if spec.requires_genx:
            latest = GenXAccountSnapshot.objects.order_by("-created_at").first()
            score = getattr(job, "jobscore", None)
            if not latest or latest.available_credits is None:
                reasons.append("GENX_BALANCE_UNVERIFIED")
            elif score is None or latest.available_credits < score.max_genx_credits:
                reasons.append("GENX_BUDGET_INSUFFICIENT")

    market_policy = MarketPolicyVersion.objects.filter(marketplace=job.marketplace).order_by("-checked_at", "-created_at").first()
    if not market_policy or not market_policy.automation_allowed:
        reasons.append("MARKET_AUTOMATION_POLICY_NOT_VERIFIED")
    elif market_policy.checked_at < timezone.now() - timedelta(days=max(1, int(os.getenv("MARKET_POLICY_MAX_AGE_DAYS", "30")))):
        reasons.append("MARKET_AUTOMATION_POLICY_STALE")
    if market_policy and not market_policy.webdock_compatible:
        reasons.append("MARKET_RUNTIME_NOT_COMPATIBLE")

    market = job.marketplace
    if not market.enabled:
        reasons.append("MARKET_DISABLED")
    if market.status != market.Status.LIVE:
        reasons.append("MARKET_NOT_LIVE")
    if not market.payout_ready:
        reasons.append("PAYOUT_NOT_READY")
    if not market.south_africa_verified:
        reasons.append("SOUTH_AFRICA_NOT_VERIFIED")

    score = getattr(job, "jobscore", None)
    expected_gross = Decimal(str(getattr(score, "recommended_offer", None) or job.reward))
    fee = (expected_gross * Decimal(str(market.fee_rate))).quantize(CENT, rounding=ROUND_HALF_UP)
    genx_cost = Decimal(str(getattr(score, "expected_genx_cost", 0) or 0))
    operational_cost = _decimal_env("EXPECTED_OPERATIONAL_COST_PER_JOB_USD", "0.10")
    if genx_cost + operational_cost > _decimal_env("MAX_EXECUTION_COST_PER_JOB_USD", "3.00"):
        reasons.append("EXECUTION_COST_ABOVE_MAXIMUM")
    expected_net = (expected_gross - fee - genx_cost - operational_cost).quantize(CENT, rounding=ROUND_HALF_UP)
    economics_confidence = Decimal(str(getattr(score, "p_accept", 0) or 0)) * Decimal(str(getattr(score, "p_payment", 0) or 0))
    confidence = min(inference_confidence, economics_confidence).quantize(Decimal("0.00001"))
    if confidence < _decimal_env("MIN_ACQUISITION_CONFIDENCE", "0.75"):
        reasons.append("ACQUISITION_CONFIDENCE_LOW")

    admission = decide_admission(
        purpose="ACQUISITION", job=job, operation=operation,
        expected_storage_bytes=_expected_storage_bytes(job), persist=persist,
    )
    reasons.extend(admission.reason_codes)
    capacity = capture_capacity(persist=persist)
    economic = evaluate_opportunity(job, capacity=capacity, capability=spec.worker_class if spec else operation)
    reasons.extend(economic.reason_codes)
    reasons = list(dict.fromkeys(reasons))
    eligible = not [reason for reason in reasons if reason not in {"AUTONOMY_OFF", "AUTONOMY_SHADOW_ONLY", "ACQUISITION_SWITCH_DISABLED"}]
    allowed = eligible and autonomy.may_acquire
    values = {
        "job": job,
        "autonomy_mode": autonomy.mode.value,
        "operation": operation,
        "worker_class": spec.worker_class if spec else "",
        "eligible": eligible,
        "allowed": allowed,
        "reason_codes": reasons,
        "expected_gross": expected_gross,
        "marketplace_fee": fee,
        "genx_cost": genx_cost,
        "operational_cost": operational_cost,
        "expected_net": expected_net,
        "confidence": confidence,
        "details": {
            "enabled_operations": sorted(enabled_operations),
            "expected_storage_bytes": _expected_storage_bytes(job),
            "resource_decision_id": str(getattr(admission, "id", "")),
            "capacity_snapshot_id": str(getattr(capacity, "id", "")),
            "growth_stage": economic.growth_stage.value,
            "utilization_state": economic.utilization_state.value,
            "risk_adjusted_profit": str(economic.risk_adjusted_profit),
            "opportunity_cost": str(economic.opportunity_cost),
            "exploration": economic.exploration,
            "reputation_investment": economic.reputation_investment,
        },
    }
    if persist:
        result = AcquisitionPreflight.objects.create(**values)
        AuditEvent.objects.create(
            severity="INFO" if allowed else "WARNING",
            event_type="job.acquisition_preflight_allowed" if allowed else "job.acquisition_preflight_blocked",
            actor="acquisition-preflight",
            metadata={"job_id": str(job.id), "preflight_id": str(result.id), "operation": operation, "reason_codes": reasons},
        )
        persist_opportunity_decision(
            job, economic, capacity=capacity, preflight=result,
            allowed=result.allowed, reason_codes=reasons,
        )
        return result
    return type("PreflightResult", (), values)()


def require_acquisition_preflight(job):
    result = run_acquisition_preflight(job)
    if not result.allowed:
        raise ValueError("acquisition preflight blocked: " + ",".join(result.reason_codes))
    return result
