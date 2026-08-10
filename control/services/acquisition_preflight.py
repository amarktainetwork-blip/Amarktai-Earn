from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from control.acquisition import paid_cost_envelope
from control.models import AcquisitionPreflight, AuditEvent, GenXAccountSnapshot, MarketPolicyVersion
from control.services.admission import decide_admission
from control.services.autonomy import acquisition_autonomy
from control.services.market_readiness import acquisition_cash_gate_required, acquisition_profile_blockers
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
    payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
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
    try:
        integration_profile = job.marketplace.integration_profile
    except Exception:
        integration_profile = None
    market_switch_name = f"{job.marketplace.slug.upper().replace('-', '_')}_AUTO_ACQUIRE_ENABLED"
    legacy_switch = os.getenv("AGENTGIGS_AUTO_APPLY_ENABLED", "0") == "1" if job.marketplace.slug == "agentgigs" else False
    configured_switch = os.getenv(market_switch_name, "1" if legacy_switch else "0") == "1"
    switch_enabled = configured_switch and (
        integration_profile.autonomous_acquisition_enabled if integration_profile is not None else True
    )
    autonomy = acquisition_autonomy(switch_enabled=switch_enabled)
    reasons.extend(autonomy.reason_codes)
    if integration_profile is not None:
        if not integration_profile.policy_verified:
            reasons.append("MARKET_ADAPTER_POLICY_NOT_VERIFIED")
        if not integration_profile.autonomous_acquisition_enabled:
            reasons.append("MARKET_AUTONOMOUS_ACQUISITION_DISABLED")
        reasons.extend(acquisition_profile_blockers(job.marketplace, integration_profile.blockers))

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
        embedded_input = any(payload.get(key) for key in ("content", "slides", "sections", "brief", "requirements", "url"))
        repository_input = operation == "technical_documentation" and any(str(payload.get(key) or "").strip() for key in ("repository_url", "repo_url", "github_url", "repository"))
        if spec.input_suffixes and not suffixes and not embedded_input and not repository_input:
            reasons.append("INPUT_TYPE_NOT_DECLARED")
        elif spec.input_suffixes and not suffixes.intersection(spec.input_suffixes):
            reasons.append("INPUT_TYPE_NOT_SUPPORTED")
        if spec.requires_genx:
            latest = GenXAccountSnapshot.objects.order_by("-created_at").first()
            score = getattr(job, "jobscore", None)
            max_genx_credits = Decimal(str(getattr(score, "max_genx_credits", 0) or 0))
            expected_genx_cost = Decimal(str(getattr(score, "expected_genx_cost", 0) or 0))
            if score is None or max_genx_credits <= 0 or expected_genx_cost <= 0:
                reasons.append("GENX_PAID_BUDGET_UNVERIFIED")
            if not latest or latest.available_credits is None:
                reasons.append("GENX_BALANCE_UNVERIFIED")
            elif Decimal(str(latest.available_credits)) < max_genx_credits:
                reasons.append("GENX_BUDGET_INSUFFICIENT")
    if operation == "public_web_extract":
        if os.getenv("PUBLIC_WEB_DATA_ENABLED", "0") != "1":
            reasons.append("PUBLIC_WEB_DATA_DISABLED")
        if payload.get("authorization_confirmed") is not True or payload.get("terms_permit") is not True:
            reasons.append("PUBLIC_WEB_POLICY_PROOF_REQUIRED")
        if not str(payload.get("purpose") or "").strip():
            reasons.append("PUBLIC_WEB_PURPOSE_REQUIRED")
    if operation == "defensive_code_review":
        if payload.get("authorization_confirmed") is not True:
            reasons.append("DEFENSIVE_REVIEW_AUTHORIZATION_REQUIRED")
        if not str(payload.get("scope") or "").strip():
            reasons.append("DEFENSIVE_REVIEW_SCOPE_REQUIRED")
        if not any(str(payload.get(key) or "").strip() for key in ("repository_url", "repo_url", "github_url", "repository")):
            reasons.append("REPOSITORY_NOT_DECLARED")
    if operation == "synthetic_dataset_generate":
        if payload.get("rights_confirmed") is not True or not isinstance(payload.get("provenance"), dict) or not payload.get("provenance"):
            reasons.append("SYNTHETIC_RIGHTS_AND_PROVENANCE_REQUIRED")
        if not isinstance(payload.get("schema"), dict) or not isinstance(payload.get("generation_plan"), dict):
            reasons.append("SYNTHETIC_SCHEMA_AND_PLAN_REQUIRED")
        if str(payload.get("mode") or "COMMISSIONED").upper() == "INVENTORY" and not (
            payload.get("inventory_demand_evidence") and payload.get("inventory_budget_authorized") is True
            and os.getenv("SYNTHETIC_SPECULATIVE_INVENTORY_ENABLED", "0") == "1"
        ):
            reasons.append("SYNTHETIC_INVENTORY_NOT_EXPLICITLY_AUTHORIZED")
    if operation == "ai_safety_evaluate":
        from control.models import BountyProgram

        program_id = payload.get("bounty_program_id")
        canonical_target = str(payload.get("canonical_target") or "")
        test_type = str(payload.get("test_type") or "").upper()
        program = BountyProgram.objects.filter(pk=program_id, status=BountyProgram.Status.ACTIVE, execution_enabled=True, automation_allowed=True).first()
        scope = program.scope_versions.filter(active=True, effective_at__lte=timezone.now(), expires_at__gt=timezone.now()).order_by("-version").first() if program else None
        if not scope:
            reasons.append("NO_SCOPE_NO_TESTING")
        else:
            if test_type not in {str(value).upper() for value in scope.allowed_test_types}:
                reasons.append("SAFETY_TEST_TYPE_NOT_PERMITTED")
            if not scope.authorized_targets.filter(canonical_target=canonical_target, active=True).exists():
                reasons.append("TARGET_NOT_IN_CURRENT_SCOPE")
        if os.getenv("SAFETY_BOUNTY_EXECUTION_ENABLED", "0") != "1":
            reasons.append("SAFETY_BOUNTY_EXECUTION_DISABLED")

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
    cash_gate_required = acquisition_cash_gate_required(market)
    if cash_gate_required:
        if not market.payout_ready:
            reasons.append("PAYOUT_NOT_READY")
        if not market.south_africa_verified:
            reasons.append("SOUTH_AFRICA_NOT_VERIFIED")

    score = getattr(job, "jobscore", None)
    expected_gross = Decimal(str(getattr(score, "recommended_offer", None) or job.reward))
    fee = (expected_gross * Decimal(str(market.fee_rate))).quantize(CENT, rounding=ROUND_HALF_UP)
    genx_cost = Decimal(str(getattr(score, "expected_genx_cost", 0) or 0))
    external_cost = Decimal(str(getattr(score, "expected_external_cost", 0) or 0))
    operational_cost = _decimal_env("EXPECTED_OPERATIONAL_COST_PER_JOB_USD", "0.10")
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
    paid_budget = paid_cost_envelope(
        expected_gross=expected_gross,
        marketplace_fee=fee,
        expected_genx_cost=genx_cost,
        expected_external_cost=external_cost,
        expected_operational_cost=operational_cost,
        risk_adjusted_profit=economic.risk_adjusted_profit - operational_cost,
        minimum_expected_profit=_decimal_env("MIN_EXPECTED_PROFIT_USD", "0.00"),
        absolute_max_paid_cost=_decimal_env("ABSOLUTE_MAX_PAID_COST_PER_JOB_USD", "250.00"),
        contingency_fraction=_decimal_env("PAID_COST_CONTINGENCY_FRACTION", "0.10"),
    )
    reasons.extend(paid_budget.reason_codes)
    expected_net = paid_budget.expected_net_profit.quantize(CENT, rounding=ROUND_HALF_UP)
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
            "expected_external_cost": str(external_cost),
            "cash_gate_required_for_acquisition": cash_gate_required,
            "platform_wallet_proving": not cash_gate_required,
            "paid_cost_envelope": {
                "semantics": "PROFITABILITY_RELATIVE_WITH_ABSOLUTE_SAFETY_CIRCUIT_BREAKER",
                "expected_paid_cost": str(paid_budget.expected_paid_cost),
                "economically_supported_paid_cost": str(paid_budget.economically_supported_paid_cost),
                "approved_paid_cost_budget": str(paid_budget.approved_paid_cost_budget),
                "absolute_safety_ceiling": str(paid_budget.absolute_safety_ceiling),
                "expected_net_profit": str(paid_budget.expected_net_profit),
                "risk_adjusted_profit": str(paid_budget.risk_adjusted_profit),
                "contingency_fraction": str(paid_budget.contingency_fraction),
                "reason_codes": list(paid_budget.reason_codes),
            },
            "exploration": economic.exploration,
            "reputation_investment": economic.reputation_investment,
            "market_adapter": integration_profile.adapter_name if integration_profile else "",
            "market_adapter_capabilities": integration_profile.capabilities if integration_profile else {},
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
