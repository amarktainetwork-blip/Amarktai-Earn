from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal("0")
CENT = Decimal("0.01")

@dataclass(frozen=True)
class EconomicsInput:
    gross_reward: Decimal
    marketplace_fee: Decimal
    p_acquire: Decimal
    p_accept: Decimal
    p_payment: Decimal
    expected_genx_cost: Decimal = ZERO
    expected_external_cost: Decimal = ZERO
    expected_compute_cost: Decimal = ZERO
    opportunity_cost: Decimal = ZERO
    estimated_worker_minutes: Decimal = Decimal("1")

@dataclass(frozen=True)
class EconomicsResult:
    net_reward: Decimal
    expected_cash: Decimal
    expected_profit: Decimal
    expected_profit_per_minute: Decimal
    expected_profit_per_genx_credit: Decimal | None


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def score_job(i: EconomicsInput) -> EconomicsResult:
    for name, probability in (("p_acquire", i.p_acquire), ("p_accept", i.p_accept), ("p_payment", i.p_payment)):
        if probability < 0 or probability > 1:
            raise ValueError(f"{name} must be between 0 and 1")
    if i.estimated_worker_minutes <= 0:
        raise ValueError("estimated_worker_minutes must be positive")

    net_reward = i.gross_reward - i.marketplace_fee
    expected_cash = net_reward * i.p_acquire * i.p_accept * i.p_payment
    expected_profit = expected_cash - i.expected_genx_cost - i.expected_external_cost - i.expected_compute_cost - i.opportunity_cost
    per_minute = expected_profit / i.estimated_worker_minutes
    per_genx = None if i.expected_genx_cost <= 0 else expected_profit / i.expected_genx_cost
    return EconomicsResult(
        net_reward=_money(net_reward),
        expected_cash=_money(expected_cash),
        expected_profit=_money(expected_profit),
        expected_profit_per_minute=_money(per_minute),
        expected_profit_per_genx_credit=None if per_genx is None else per_genx.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
    )
