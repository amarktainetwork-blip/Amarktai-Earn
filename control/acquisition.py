from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AcquisitionThresholds:
    min_expected_profit: Decimal = Decimal("0.00")
    min_expected_profit_per_minute: Decimal = Decimal("0.00")
    absolute_max_paid_cost: Decimal = Decimal("250.00")
    paid_cost_contingency_fraction: Decimal = Decimal("0.10")


@dataclass(frozen=True)
class MarketGate:
    enabled: bool
    status: str
    payout_ready: bool
    south_africa_verified: bool


@dataclass(frozen=True)
class ScoreGate:
    expected_profit: Decimal
    expected_profit_per_minute: Decimal
    expected_genx_cost: Decimal
    expected_gross: Decimal
    marketplace_fee: Decimal = Decimal("0")
    expected_external_cost: Decimal = Decimal("0")
    expected_operational_cost: Decimal = Decimal("0")


@dataclass(frozen=True)
class PaidCostEnvelope:
    allowed: bool
    reason_codes: tuple[str, ...]
    expected_paid_cost: Decimal
    economically_supported_paid_cost: Decimal
    approved_paid_cost_budget: Decimal
    absolute_safety_ceiling: Decimal
    expected_net_profit: Decimal
    risk_adjusted_profit: Decimal
    contingency_fraction: Decimal


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason_codes: tuple[str, ...]


def paid_cost_envelope(
    *,
    expected_gross: Decimal,
    marketplace_fee: Decimal,
    expected_genx_cost: Decimal,
    expected_external_cost: Decimal,
    expected_operational_cost: Decimal,
    risk_adjusted_profit: Decimal,
    minimum_expected_profit: Decimal = Decimal("0"),
    absolute_max_paid_cost: Decimal = Decimal("250.00"),
    contingency_fraction: Decimal = Decimal("0.10"),
) -> PaidCostEnvelope:
    values = (
        expected_gross,
        marketplace_fee,
        expected_genx_cost,
        expected_external_cost,
        expected_operational_cost,
        minimum_expected_profit,
    )
    reasons: list[str] = []
    if any(value < 0 for value in values) or expected_gross <= 0 or marketplace_fee > expected_gross:
        reasons.append("ECONOMIC_INPUT_INVALID")
    if absolute_max_paid_cost <= 0:
        reasons.append("PAID_COST_SAFETY_CEILING_INVALID")
    if contingency_fraction < 0 or contingency_fraction > 1:
        reasons.append("PAID_COST_CONTINGENCY_INVALID")

    expected_paid_cost = expected_genx_cost + expected_external_cost + expected_operational_cost
    economically_supported = max(Decimal("0"), expected_gross - marketplace_fee - minimum_expected_profit)
    planned_budget = expected_paid_cost * (Decimal("1") + max(Decimal("0"), min(Decimal("1"), contingency_fraction)))
    approved_budget = max(Decimal("0"), min(planned_budget, economically_supported, absolute_max_paid_cost))
    expected_net_profit = expected_gross - marketplace_fee - expected_paid_cost
    if expected_net_profit <= minimum_expected_profit:
        reasons.append("EXPECTED_NET_PROFIT_NOT_POSITIVE")
    if risk_adjusted_profit <= minimum_expected_profit:
        reasons.append("RISK_ADJUSTED_PROFIT_NOT_POSITIVE")
    if expected_paid_cost > absolute_max_paid_cost:
        reasons.append("PAID_COST_EMERGENCY_CEILING_EXCEEDED")
    elif expected_paid_cost > economically_supported:
        reasons.append("PAID_COST_EXCEEDS_PROFITABLE_ENVELOPE")

    return PaidCostEnvelope(
        allowed=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
        expected_paid_cost=expected_paid_cost,
        economically_supported_paid_cost=economically_supported,
        approved_paid_cost_budget=approved_budget,
        absolute_safety_ceiling=absolute_max_paid_cost,
        expected_net_profit=expected_net_profit,
        risk_adjusted_profit=risk_adjusted_profit,
        contingency_fraction=contingency_fraction,
    )


def acquisition_gate(market: MarketGate, score: ScoreGate, thresholds: AcquisitionThresholds) -> GateDecision:
    reasons: list[str] = []
    if not market.enabled:
        reasons.append("MARKET_DISABLED")
    if market.status != "LIVE":
        reasons.append(f"MARKET_{market.status}")
    if not market.payout_ready:
        reasons.append("PAYOUT_NOT_READY")
    if not market.south_africa_verified:
        reasons.append("SOUTH_AFRICA_NOT_VERIFIED")
    if score.expected_profit < thresholds.min_expected_profit:
        reasons.append("EXPECTED_PROFIT_TOO_LOW")
    if score.expected_profit_per_minute < thresholds.min_expected_profit_per_minute:
        reasons.append("PROFIT_PER_MINUTE_TOO_LOW")
    envelope = paid_cost_envelope(
        expected_gross=score.expected_gross,
        marketplace_fee=score.marketplace_fee,
        expected_genx_cost=score.expected_genx_cost,
        expected_external_cost=score.expected_external_cost,
        expected_operational_cost=score.expected_operational_cost,
        risk_adjusted_profit=score.expected_profit,
        minimum_expected_profit=thresholds.min_expected_profit,
        absolute_max_paid_cost=thresholds.absolute_max_paid_cost,
        contingency_fraction=thresholds.paid_cost_contingency_fraction,
    )
    reasons.extend(envelope.reason_codes)
    return GateDecision(allowed=not reasons, reason_codes=tuple(reasons))
