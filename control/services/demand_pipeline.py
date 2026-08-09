from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any

from control.economics import EconomicsInput
from control.models import AuditEvent, Job
from control.services.acquisition_preflight import infer_operation, run_acquisition_preflight
from control.services.jobs import score_and_persist
from workers.registry import WorkerRegistryError, operation_spec


CENT = Decimal("0.01")

BUYER_DEMAND = "BUYER_DEMAND"
SELLER_SUPPLY_LISTING = "SELLER_SUPPLY_LISTING"
TEST_OR_SYNTHETIC = "TEST_OR_SYNTHETIC"
INCOMPLETE_REQUIREMENTS = "INCOMPLETE_REQUIREMENTS"
UNFUNDED = "UNFUNDED"
UNSUPPORTED = "UNSUPPORTED"
UNKNOWN = "UNKNOWN"

DEMAND_CLASSIFICATIONS = (
    BUYER_DEMAND,
    SELLER_SUPPLY_LISTING,
    TEST_OR_SYNTHETIC,
    INCOMPLETE_REQUIREMENTS,
    UNFUNDED,
    UNSUPPORTED,
    UNKNOWN,
)


@dataclass(frozen=True)
class DemandQualification:
    classification: str
    actionable: bool
    reason_codes: tuple[str, ...]
    evidence: dict[str, Any]


@dataclass(frozen=True)
class MarketEconomics:
    percentage_fee_rate: Decimal
    fixed_transaction_fee: Decimal
    payout_cost_rate: Decimal
    fx_cost_rate: Decimal
    chargeback_reserve_rate: Decimal
    external_execution_cost: Decimal
    settlement_delay_days: int | None
    execution_placement: str
    verified: bool
    reason_codes: tuple[str, ...]

    @property
    def total_variable_deduction_rate(self) -> Decimal:
        return (
            self.percentage_fee_rate
            + self.payout_cost_rate
            + self.fx_cost_rate
            + self.chargeback_reserve_rate
        )


def _decimal_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except Exception:
        return Decimal(default)


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _structured_values(payload: dict, keys: tuple[str, ...]) -> set[str]:
    values: set[str] = set()
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (list, tuple, set)):
            values.update(_text(item) for item in value if _text(item))
        elif _text(value):
            values.add(_text(value))
    return values


def _explicit_false(payload: dict, keys: tuple[str, ...]) -> bool:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if value is False or _text(value) in {"false", "0", "no", "unfunded", "not_funded", "not funded"}:
            return True
    return False


def _nonempty(payload: dict, keys: tuple[str, ...]) -> bool:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, tuple, dict)) and len(value) > 0:
            return True
    return False


def qualify_payload(payload: dict, *, title: str = "", reward: Decimal = Decimal("0")) -> DemandQualification:
    """Classify marketplace inventory before economics.

    Structured market evidence wins. Text is used only as supporting evidence, so
    ambiguous listings fail closed instead of being promoted into buyer demand.
    """
    payload = payload if isinstance(payload, dict) else {}
    structured = _structured_values(
        payload,
        (
            "source_type", "sourceType", "listing_type", "listingType", "type", "kind",
            "job_mode", "jobMode", "relationship", "poster_type", "posterType", "role",
        ),
    )
    status_values = _structured_values(payload, ("status", "state", "funding_state", "fundingState", "escrow_state", "escrowState"))
    combined = " ".join(
        part
        for part in (
            _text(title),
            _text(payload.get("title")),
            _text(payload.get("description")),
            _text(payload.get("requirements")),
            _text(payload.get("instructions")),
        )
        if part
    )

    test_values = {"test", "testing", "synthetic", "demo", "sandbox", "sample"}
    if structured.intersection(test_values) or any(payload.get(key) is True for key in ("is_test", "isTest", "synthetic", "isSynthetic")):
        return DemandQualification(TEST_OR_SYNTHETIC, False, ("TEST_OR_SYNTHETIC",), {"structured": sorted(structured)})

    if _explicit_false(payload, ("requirements_complete", "requirementsComplete", "inputs_complete", "inputsComplete")) or _nonempty(payload, ("missing_inputs", "missingInputs", "missing_requirements", "missingRequirements")):
        return DemandQualification(INCOMPLETE_REQUIREMENTS, False, ("INCOMPLETE_REQUIREMENTS",), {"structured": sorted(structured)})

    if reward <= 0 or _explicit_false(
        payload,
        (
            "posterFunded", "poster_funded", "funded", "isFunded", "funding_confirmed",
            "fundingConfirmed", "escrowFunded", "escrow_funded",
        ),
    ) or status_values.intersection({"unfunded", "not_funded", "not funded", "payment_required"}):
        reasons = ["UNFUNDED"]
        if reward <= 0:
            reasons.append("NON_POSITIVE_REWARD")
        return DemandQualification(UNFUNDED, False, tuple(reasons), {"structured": sorted(structured), "status": sorted(status_values)})

    seller_types = {
        "service", "service_listing", "service listing", "seller", "provider", "agent_profile",
        "agent profile", "offer", "supply", "freelancer_profile", "freelancer profile",
    }
    buyer_types = {
        "job", "posted_job", "posted job", "task", "bounty", "request", "project",
        "buyer_request", "buyer request", "work_order", "work order",
    }
    seller_language = any(
        marker in combined
        for marker in (
            "i offer ", "we offer ", "i build ", "we build ", "i provide ", "we provide ",
            "hire me", "available for hire", "my services", "our services",
        )
    )
    requirements_evidence = _nonempty(
        payload,
        ("acceptanceCriteria", "acceptance_criteria", "requirements", "instructions", "deliverables", "requiredInputs", "required_inputs"),
    )
    if structured.intersection(seller_types) or (seller_language and not requirements_evidence and not structured.intersection(buyer_types)):
        return DemandQualification(
            SELLER_SUPPLY_LISTING,
            False,
            ("SELLER_SUPPLY_LISTING",),
            {"structured": sorted(structured), "seller_language": seller_language, "requirements_evidence": requirements_evidence},
        )

    buyer_language = any(
        marker in combined
        for marker in (
            "need ", "looking for ", "seeking ", "please ", "build ", "create ", "convert ",
            "normalize ", "normalise ", "analyze ", "analyse ", "generate ", "produce ", "fix ",
        )
    )
    claimable_evidence = _nonempty(payload, ("acceptanceCriteria", "acceptance_criteria")) or any(
        _text(payload.get(key)) in {"open", "available", "claimable", "posted", "active"}
        for key in ("status", "state", "claim_state", "claimState")
    )
    structured_buyer = bool(structured.intersection(buyer_types))
    if structured_buyer or (requirements_evidence and (buyer_language or claimable_evidence)):
        return DemandQualification(
            BUYER_DEMAND,
            True,
            ("QUALIFIED_BUYER_DEMAND",),
            {
                "structured": sorted(structured),
                "requirements_evidence": requirements_evidence,
                "buyer_language": buyer_language,
                "claimable_evidence": claimable_evidence,
            },
        )

    return DemandQualification(
        UNKNOWN,
        False,
        ("DEMAND_CLASSIFICATION_AMBIGUOUS",),
        {
            "structured": sorted(structured),
            "requirements_evidence": requirements_evidence,
            "buyer_language": buyer_language,
            "claimable_evidence": claimable_evidence,
        },
    )


def qualify_job(job: Job, *, persist: bool = True) -> DemandQualification:
    payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    qualification = qualify_payload(payload, title=job.title, reward=Decimal(str(job.reward)))
    if persist:
        next_payload = dict(payload)
        next_payload["demand_qualification"] = {
            "classification": qualification.classification,
            "actionable": qualification.actionable,
            "reason_codes": list(qualification.reason_codes),
            "evidence": qualification.evidence,
        }
        if next_payload != payload:
            job.normalized_payload = next_payload
            job.save(update_fields=["normalized_payload", "updated_at"])
        AuditEvent.objects.create(
            event_type="job.demand_qualified",
            actor="market-scout",
            metadata={
                "job_id": str(job.id),
                "market": job.marketplace.slug,
                "classification": qualification.classification,
                "actionable": qualification.actionable,
                "reason_codes": list(qualification.reason_codes),
            },
        )
    return qualification


def market_economics(job: Job) -> MarketEconomics:
    reasons: list[str] = []
    try:
        profile = job.marketplace.integration_profile
    except Exception:
        profile = None
    evidence = profile.evidence if profile is not None and isinstance(profile.evidence, dict) else {}
    catalog = evidence.get("catalog_truth") if isinstance(evidence.get("catalog_truth"), dict) else {}
    economics = catalog.get("economics") if isinstance(catalog.get("economics"), dict) else {}

    percentage = economics.get("percentage_fee_rate")
    verified = percentage not in (None, "")
    if verified:
        percentage_rate = _decimal(percentage)
    elif Decimal(str(job.marketplace.fee_rate or 0)) > 0:
        percentage_rate = Decimal(str(job.marketplace.fee_rate))
        verified = True
    else:
        percentage_rate = _decimal_env("MARKET_UNVERIFIED_FEE_RESERVE_RATE", "0.30")
        reasons.append("MARKET_FEE_PROFILE_UNVERIFIED")

    payout_rate = economics.get("payout_cost_rate")
    if payout_rate in (None, ""):
        payout_cost_rate = _decimal_env("MARKET_UNVERIFIED_PAYOUT_RESERVE_RATE", "0.05")
        reasons.append("MARKET_PAYOUT_COST_UNVERIFIED")
        verified = False
    else:
        payout_cost_rate = _decimal(payout_rate)

    fx_rate = _decimal(economics.get("fx_cost_rate"), "0")
    chargeback_rate = _decimal(economics.get("chargeback_reserve_rate"), "0")
    fixed_fee = _decimal(economics.get("fixed_transaction_fee"), "0")
    external_cost = _decimal(
        economics.get("external_execution_cost_usd"),
        str(_decimal_env("MARKET_EXPECTED_EXTERNAL_COST_USD", "0")),
    )
    total_rate = percentage_rate + payout_cost_rate + fx_rate + chargeback_rate
    if any(value < 0 for value in (percentage_rate, payout_cost_rate, fx_rate, chargeback_rate, fixed_fee, external_cost)) or total_rate >= 1:
        reasons.append("MARKET_ECONOMICS_INVALID")
        verified = False

    delay = economics.get("settlement_delay_days")
    try:
        settlement_delay_days = None if delay in (None, "") else max(0, int(delay))
    except Exception:
        settlement_delay_days = None
        reasons.append("SETTLEMENT_DELAY_INVALID")
        verified = False

    placement = str(catalog.get("execution_placement") or "UNVERIFIED").upper()
    return MarketEconomics(
        percentage_fee_rate=percentage_rate,
        fixed_transaction_fee=fixed_fee,
        payout_cost_rate=payout_cost_rate,
        fx_cost_rate=fx_rate,
        chargeback_reserve_rate=chargeback_rate,
        external_execution_cost=external_cost,
        settlement_delay_days=settlement_delay_days,
        execution_placement=placement,
        verified=verified and not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def shadow_score_job(job: Job, qualification: DemandQualification | None = None) -> dict[str, Any]:
    qualification = qualification or qualify_job(job)
    if not qualification.actionable:
        return {
            "scored": False,
            "classification": qualification.classification,
            "reason_codes": list(qualification.reason_codes),
        }

    terms = market_economics(job)
    gross = Decimal(str(job.reward))
    marketplace_fee = (
        gross * terms.total_variable_deduction_rate + terms.fixed_transaction_fee
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    if marketplace_fee >= gross:
        return {
            "scored": False,
            "classification": qualification.classification,
            "reason_codes": list(dict.fromkeys((*qualification.reason_codes, *terms.reason_codes, "MARKET_DEDUCTIONS_EXCEED_REWARD"))),
        }

    operation, _, _ = infer_operation(job)
    expected_genx = Decimal("0")
    if operation:
        try:
            if operation_spec(operation).requires_genx:
                expected_genx = _decimal_env("MARKET_EXPECTED_GENX_COST_USD", "0.25")
        except WorkerRegistryError:
            expected_genx = _decimal_env("MARKET_EXPECTED_GENX_COST_USD", "0.25")
    else:
        expected_genx = _decimal_env("MARKET_EXPECTED_GENX_COST_USD", "0.25")

    economics = EconomicsInput(
        gross_reward=gross,
        marketplace_fee=marketplace_fee,
        p_acquire=_decimal_env("MARKET_P_ACQUIRE_PRIOR", "0.10"),
        p_accept=_decimal_env("MARKET_P_ACCEPT_PRIOR", "0.80"),
        p_payment=_decimal_env("MARKET_P_PAYMENT_PRIOR", "0.85"),
        expected_genx_cost=expected_genx,
        expected_external_cost=terms.external_execution_cost,
        expected_compute_cost=_decimal_env("EXPECTED_OPERATIONAL_COST_PER_JOB_USD", "0.10"),
        estimated_worker_minutes=_decimal_env("MARKET_ESTIMATED_MINUTES_PRIOR", "60"),
    )
    reasons = list(dict.fromkeys((
        "GENERIC_SHADOW_SCORE",
        *qualification.reason_codes,
        *terms.reason_codes,
    )))
    score = score_and_persist(
        job,
        economics,
        decision="WATCH",
        reason_codes=reasons,
        recommended_offer=gross,
    )
    preflight = run_acquisition_preflight(job)
    score.decision = "WATCH"
    score.reason_codes = list(dict.fromkeys((*reasons, *preflight.reason_codes)))
    score.save(update_fields=["decision", "reason_codes", "updated_at"])
    return {
        "scored": True,
        "classification": qualification.classification,
        "score_id": str(score.id),
        "preflight_id": str(preflight.id),
        "eligible": bool(preflight.eligible),
        "allowed": bool(preflight.allowed),
        "reason_codes": list(score.reason_codes),
        "market_economics_verified": terms.verified,
        "execution_placement": terms.execution_placement,
    }


def qualify_and_shadow_score(job: Job) -> dict[str, Any]:
    qualification = qualify_job(job)
    return shadow_score_job(job, qualification)
