from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

ZERO = Decimal("0")


def _parameter_names(payload: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            names.add(str(key).casefold())
            if str(key).casefold() in {"name", "key", "field", "parameter"} and isinstance(value, str):
                names.add(value.casefold())
            names.update(_parameter_names(value))
    elif isinstance(payload, list):
        for value in payload:
            names.update(_parameter_names(value))
    return names


def model_parameter_names(payload: Any) -> set[str]:
    """Return normalized parameter names exposed by a provider catalogue row."""
    return _parameter_names(payload)


_MODEL_PARAMETER_ALIASES: dict[str, tuple[str, ...]] = {
    "prompt": ("prompt", "input_text", "text_prompt", "instruction"),
    "width": ("width", "image_width", "output_width"),
    "height": ("height", "image_height", "output_height"),
    "duration_seconds": ("duration_seconds", "duration", "seconds"),
    "voice": ("voice", "voice_id", "speaker", "speaker_id"),
    "language": ("language", "language_code", "target_language", "locale"),
}


def build_model_params(
    model_payload: Any,
    canonical_params: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Map canonical inputs to a published schema without encoding model IDs."""
    names = model_parameter_names(model_payload)
    schema_published = bool(names & {
        "parameters", "params", "input_schema", "request_schema", "json_schema", "properties",
    })
    mapped: dict[str, Any] = {}
    for canonical_name, value in canonical_params.items():
        aliases = _MODEL_PARAMETER_ALIASES.get(canonical_name, (canonical_name,))
        selected_name = next((name for name in aliases if name in names), None)
        if selected_name:
            mapped[selected_name] = value
        elif not schema_published:
            mapped[canonical_name] = value
        elif canonical_name in required:
            return None
    return mapped


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _list_records(value: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, str) and item.strip():
            result.append({"id": item.strip()})
    return result


def records(payload: Any, keys: Iterable[str] = ("models", "data", "items", "pricing")) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return _list_records(payload)
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return _list_records(value)
        if isinstance(value, dict):
            result = []
            for item_key, item_value in value.items():
                if isinstance(item_value, dict):
                    row = dict(item_value)
                    row.setdefault("id", item_key)
                    result.append(row)
            if result:
                return result
    return []


def model_id(row: dict[str, Any]) -> str:
    for key in ("id", "model_id", "model", "slug"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def pricing_index(payload: Any) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    if isinstance(payload, dict):
        direct = payload.get("pricing")
        if isinstance(direct, dict):
            for key, value in direct.items():
                if isinstance(value, dict):
                    indexed[str(key)] = dict(value)
    for row in records(payload, ("models", "data", "items", "pricing")):
        key = model_id(row)
        if key:
            indexed[key] = row
    return indexed


def price_hint(payload: Any) -> Decimal | None:
    """Return only an explicit per-call credit price, never an arbitrary nested minimum."""
    if not isinstance(payload, dict):
        return None
    containers = [payload]
    if isinstance(payload.get("pricing"), dict):
        containers.append(payload["pricing"])
    for container in containers:
        for key in ("credits_per_call", "credit_cost_per_call", "fixed_credits"):
            value = _decimal(container.get(key))
            if value is not None and value > ZERO:
                return value
    return None


def pricing_credit_estimate(
    payload: Any,
    params: dict[str, Any] | None,
    *,
    historical_average: Decimal | None,
    reserved_envelope: Decimal,
) -> Decimal:
    """Estimate credits only when the provider metric and its request unit are both known."""
    params = params if isinstance(params, dict) else {}
    pricing = payload.get("pricing") if isinstance(payload, dict) and isinstance(payload.get("pricing"), dict) else payload
    if isinstance(pricing, dict):
        fixed = price_hint(pricing)
        if fixed is not None:
            return fixed
        metric_contracts = (
            (("credits_per_image", "credit_per_image"), ("image_count", "num_images", "n"), Decimal("1")),
            (("credits_per_audio_second", "credits_per_second"), ("audio_seconds", "duration_seconds"), None),
            (("credits_per_video_second",), ("video_seconds", "duration_seconds"), None),
            (("credits_per_minute",), ("duration_minutes",), None),
            (("credits_per_1000_tokens", "credits_per_1k_tokens"), ("estimated_tokens", "max_tokens"), None),
        )
        for rate_keys, unit_keys, default_units in metric_contracts:
            rate = next((_decimal(pricing.get(key)) for key in rate_keys if key in pricing), None)
            units = next((_decimal(params.get(key)) for key in unit_keys if key in params), default_units)
            if rate is not None and rate > ZERO and units is not None and units > ZERO:
                divisor = Decimal("1000") if any("1000" in key or "1k" in key for key in rate_keys) else Decimal("1")
                return (rate * units) / divisor
        input_rate = _decimal(pricing.get("input_credits_per_1000_tokens"))
        output_rate = _decimal(pricing.get("output_credits_per_1000_tokens"))
        input_tokens = _decimal(params.get("estimated_input_tokens"))
        output_tokens = _decimal(params.get("max_output_tokens"))
        if all(value is not None and value >= ZERO for value in (input_rate, output_rate, input_tokens, output_tokens)) and (input_tokens or output_tokens):
            return ((input_rate * input_tokens) + (output_rate * output_tokens)) / Decimal("1000")
        input_rate = _decimal(pricing.get("input_credits_per_million"))
        output_rate = _decimal(pricing.get("output_credits_per_million"))
        if all(value is not None and value >= ZERO for value in (input_rate, output_rate, input_tokens, output_tokens)) and (input_tokens or output_tokens):
            return ((input_rate * input_tokens) + (output_rate * output_tokens)) / Decimal("1000000")
    if historical_average is not None and historical_average > ZERO:
        return historical_average
    return Decimal(reserved_envelope)


def _find_decimal(payload: Any, candidate_keys: tuple[str, ...]) -> Decimal | None:
    if isinstance(payload, dict):
        for key in candidate_keys:
            if key in payload:
                number = _decimal(payload[key])
                if number is not None:
                    return number
        for value in payload.values():
            nested = _find_decimal(value, candidate_keys)
            if nested is not None:
                return nested
    return None


def available_credits(payload: Any) -> Decimal | None:
    return _find_decimal(payload, ("available_credits", "availableCredits", "balance", "credits", "available"))


def usage_credits(payload: Any) -> Decimal | None:
    if not isinstance(payload, dict):
        return None
    # Prefer explicit billed/charged credit fields before broad nested usage fields.
    direct = _find_decimal(payload, ("credits_charged", "credits_used", "charged_credits", "billed_credits"))
    if direct is not None:
        return direct
    for container in ("usage", "billing", "cost"):
        nested = payload.get(container)
        if isinstance(nested, dict):
            value = _find_decimal(nested, ("credits", "total_credits", "cost_credits"))
            if value is not None:
                return value
    return None


def result_url(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("result_url", "url", "output_url"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("url", "result_url", "output_url"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


@dataclass(frozen=True)
class ModelCandidate:
    model_id: str
    price_hint: Decimal | None = None
    expected_credits: Decimal = ZERO
    attempts: int = 0
    accepted: int = 0
    profit: Decimal = ZERO
    credits: Decimal = ZERO
    successful_executions: int = 0
    qa_accepted: int = 0
    qa_rejected: int = 0
    repair_required: int = 0
    failures: int = 0
    provider_failures: int = 0
    retry_count: int = 0
    total_repair_cost: Decimal = ZERO
    net_profit: Decimal = ZERO

    @property
    def acceptance_rate(self) -> Decimal:
        return ZERO if self.attempts <= 0 else Decimal(self.accepted) / Decimal(self.attempts)

    @property
    def profit_per_credit(self) -> Decimal | None:
        return None if self.credits <= ZERO else self.profit / self.credits

    @property
    def qa_acceptance_probability(self) -> Decimal:
        observed = self.qa_accepted + self.qa_rejected
        # Conservative Beta(1,1) smoothing avoids treating one lucky pass as certainty.
        return Decimal(self.qa_accepted + 1) / Decimal(observed + 2)

    @property
    def repair_probability(self) -> Decimal:
        return Decimal(self.repair_required + 1) / Decimal(max(self.attempts, 0) + 2)

    @property
    def failure_probability(self) -> Decimal:
        return Decimal(self.failures + self.provider_failures + 1) / Decimal(max(self.attempts, 0) + 2)

    @property
    def average_credits(self) -> Decimal:
        return ZERO if self.attempts <= 0 else self.credits / Decimal(self.attempts)

    @property
    def average_repair_cost(self) -> Decimal:
        return ZERO if self.repair_required <= 0 else self.total_repair_cost / Decimal(self.repair_required)


@dataclass(frozen=True)
class EconomicRoute:
    candidate: ModelCandidate
    expected_net_profit: Decimal | None
    expected_total_cost: Decimal | None
    expected_credits: Decimal
    non_currency_score: Decimal
    quality_probability: Decimal
    exploration: bool
    cost_basis: str


def route_models(
    candidates: list[ModelCandidate],
    *,
    expected_revenue: Decimal,
    non_genx_cost: Decimal = ZERO,
    required_quality: Decimal = Decimal("0.80"),
    max_genx_credits: Decimal | None = None,
    monetary_cost_per_credit: Decimal | None = None,
    allow_exploration: bool = False,
    exploration_fraction: Decimal = Decimal("0.05"),
) -> list[EconomicRoute]:
    """Rank provider-eligible models without turning missing local history into a capability gate.

    ``allow_exploration`` and ``exploration_fraction`` remain in the public contract for
    backwards compatibility and audit callers. A live GenX model does not need local
    ModelStat history before its first legitimate use: cold-start models are routed
    under the same real credit ceiling and monetary profitability controls as proven
    models, and are labelled ``exploration`` only as an audit/learning fact.
    """
    routes: list[EconomicRoute] = []
    for candidate in candidates:
        unproven = candidate.attempts == 0
        # No local evidence means unknown, not forbidden. Until real QA exists, use
        # the task's required quality as the cold-start expectation; actual outcomes
        # immediately replace that assumption through ModelStat on later routes.
        quality = required_quality if unproven else candidate.qa_acceptance_probability
        if not unproven and quality < required_quality:
            continue
        expected_credits = candidate.expected_credits or candidate.average_credits or candidate.price_hint or ZERO
        expected_repair_credits = candidate.repair_probability * expected_credits
        expected_retry_credits = candidate.failure_probability * expected_credits
        total_credits = expected_credits + expected_repair_credits + expected_retry_credits
        if expected_credits <= ZERO:
            continue
        if max_genx_credits is not None and total_credits > max_genx_credits:
            continue
        exploration = unproven
        success_probability = max(ZERO, Decimal("1") - candidate.failure_probability)
        non_currency_score = (quality * success_probability) / max(total_credits, Decimal("0.00000001"))
        expected_net: Decimal | None = None
        expected_total_cost: Decimal | None = None
        cost_basis = "CREDIT_EFFICIENCY"
        if monetary_cost_per_credit is not None:
            expected_genx_money = expected_credits * monetary_cost_per_credit
            historical_repair_money = candidate.average_repair_cost
            expected_repair_money = candidate.repair_probability * (historical_repair_money or expected_genx_money)
            expected_retry_money = candidate.failure_probability * expected_genx_money
            expected_total_cost = expected_genx_money + expected_repair_money + expected_retry_money + non_genx_cost
            expected_net = (expected_revenue * quality) - expected_total_cost
            if expected_net <= ZERO:
                continue
            cost_basis = "AUTHORITATIVE_MONETARY_VALUATION"
        routes.append(EconomicRoute(
            candidate=candidate,
            expected_net_profit=expected_net,
            expected_total_cost=expected_total_cost,
            expected_credits=total_credits,
            non_currency_score=non_currency_score,
            quality_probability=quality,
            exploration=exploration,
            cost_basis=cost_basis,
        ))
    return sorted(
        routes,
        key=lambda route: (
            0 if route.expected_net_profit is not None else 1,
            -(route.expected_net_profit or ZERO),
            -route.non_currency_score,
            -route.quality_probability,
            route.expected_total_cost if route.expected_total_cost is not None else Decimal("999999999"),
            route.candidate.model_id,
        ),
    )


def rank_models(candidates: list[ModelCandidate]) -> list[ModelCandidate]:
    def key(candidate: ModelCandidate):
        profit_per_credit = candidate.profit_per_credit
        # Proven positive economics outrank exploration. Unproven models outrank
        # models with known non-positive economics so bad history is not rewarded.
        if candidate.attempts > 0 and profit_per_credit is not None and profit_per_credit > ZERO:
            band = 2
        elif candidate.attempts == 0:
            band = 1
        else:
            band = 0
        ppc = profit_per_credit if profit_per_credit is not None else Decimal("-999999999")
        price = candidate.price_hint if candidate.price_hint is not None else Decimal("999999999")
        return (-band, -ppc, -candidate.acceptance_rate, price, candidate.model_id)

    return sorted(candidates, key=key)


def effective_reserved_credits(calls: Iterable[tuple[Any, ...]]) -> Decimal:
    """Sum charged credits, or estimates while a non-terminal call is still reserved."""
    total = ZERO
    for row in calls:
        actual_value, estimated_value, status = row[:3]
        metadata = row[3] if len(row) > 3 and isinstance(row[3], dict) else {}
        status_upper = str(status).upper()
        if status_upper in {"FAILED", "CANCELLED"}:
            continue
        actual = _decimal(actual_value) or ZERO
        estimated = _decimal(estimated_value) or ZERO
        billing_truth = str(metadata.get("billing_truth") or "").upper()
        if billing_truth == "ACTUAL":
            total += actual
        else:
            total += actual if actual > ZERO else estimated
    return total


def assert_credit_budget(*, already_reserved: Decimal, estimated: Decimal, call_limit: Decimal, job_limit: Decimal) -> None:
    if estimated <= ZERO:
        raise ValueError("estimated credits must be positive")
    if call_limit <= ZERO:
        raise ValueError("call credit limit must be positive")
    if job_limit <= ZERO:
        raise ValueError("job GenX credit budget must be positive")
    if estimated > call_limit:
        raise ValueError("estimated credits exceed call limit")
    if already_reserved + estimated > job_limit:
        raise ValueError("estimated credits exceed remaining job budget")
