from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AcquisitionThresholds:
    min_expected_profit: Decimal = Decimal("1.00")
    min_expected_profit_per_minute: Decimal = Decimal("0.05")
    max_genx_cost: Decimal = Decimal("2.00")


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


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason_codes: tuple[str, ...]


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
    if score.expected_genx_cost > thresholds.max_genx_cost:
        reasons.append("GENX_BUDGET_TOO_HIGH")
    return GateDecision(allowed=not reasons, reason_codes=tuple(reasons))
