from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

ZERO = Decimal("0")


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


def _numeric_price_candidates(value: Any, parent_key: str = "") -> list[Decimal]:
    candidates: list[Decimal] = []
    key = parent_key.lower()
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            candidates.extend(_numeric_price_candidates(child_value, str(child_key)))
    elif isinstance(value, list):
        for child in value:
            candidates.extend(_numeric_price_candidates(child, parent_key))
    elif any(token in key for token in ("credit", "price", "cost")):
        number = _decimal(value)
        if number is not None and number >= ZERO:
            candidates.append(number)
    return candidates


def price_hint(payload: Any) -> Decimal | None:
    candidates = [value for value in _numeric_price_candidates(payload) if value > ZERO]
    return min(candidates) if candidates else None


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
    expected_net_profit: Decimal
    expected_total_cost: Decimal
    quality_probability: Decimal
    exploration: bool


def route_models(
    candidates: list[ModelCandidate],
    *,
    expected_revenue: Decimal,
    non_genx_cost: Decimal = ZERO,
    required_quality: Decimal = Decimal("0.80"),
    max_genx_cost: Decimal | None = None,
    allow_exploration: bool = False,
    exploration_fraction: Decimal = Decimal("0.05"),
) -> list[EconomicRoute]:
    """Rank task-scoped models by quality-constrained expected net profit."""
    routes: list[EconomicRoute] = []
    for candidate in candidates:
        unproven = candidate.attempts == 0
        if unproven and not allow_exploration:
            continue
        quality = candidate.qa_acceptance_probability
        if not unproven and quality < required_quality:
            continue
        expected_genx = candidate.average_credits
        if expected_genx <= ZERO:
            expected_genx = candidate.price_hint or ZERO
        expected_repair = candidate.repair_probability * (
            candidate.average_repair_cost or expected_genx
        )
        expected_retry = candidate.failure_probability * expected_genx
        total_cost = expected_genx + expected_repair + expected_retry + non_genx_cost
        if max_genx_cost is not None and expected_genx + expected_repair + expected_retry > max_genx_cost:
            continue
        expected_net = (expected_revenue * quality) - total_cost
        exploration = unproven
        if exploration and expected_genx > expected_revenue * exploration_fraction:
            continue
        if expected_net <= ZERO:
            continue
        routes.append(EconomicRoute(candidate, expected_net, total_cost, quality, exploration))
    return sorted(
        routes,
        key=lambda route: (
            -route.expected_net_profit,
            -route.quality_probability,
            route.expected_total_cost,
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
