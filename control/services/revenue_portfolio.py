from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from django.db import transaction

from control.models import AuditEvent, Job, PortfolioDecision, ServiceOffering


SIX = Decimal("0.000001")
REVENUE_CHANNEL_VALUES = {
    "POSTED_JOB", "BOUNTY", "SERVICE_LISTING", "PAY_PER_CALL_API",
    "PROJECT_HIRE", "SUBSCRIPTION", "MANUAL_STOREFRONT", "OFFHOST_SETTLEMENT",
}


def _probability(value) -> Decimal:
    try:
        parsed = Decimal(str(value or 0))
    except Exception:
        parsed = Decimal("0")
    return min(Decimal("1"), max(Decimal("0"), parsed))


@dataclass(frozen=True)
class PortfolioCandidate:
    job_id: str
    source_type: str
    revenue_channel: str
    expected_net_profit: Decimal
    risk_adjusted_profit: Decimal
    productive_minutes: Decimal
    payout_probability: Decimal
    acceptance_probability: Decimal
    deadline_pressure: Decimal = Decimal("0")
    reputation_value: Decimal = Decimal("0")
    learning_value: Decimal = Decimal("0")
    market_concentration: Decimal = Decimal("0")
    payment_risk: Decimal = Decimal("0")

    @property
    def profit_per_minute(self) -> Decimal:
        return self.risk_adjusted_profit / max(Decimal("1"), self.productive_minutes)


@dataclass(frozen=True)
class RankedCandidate:
    candidate: PortfolioCandidate
    score: Decimal
    rank: int
    selected: bool


def portfolio_score(candidate: PortfolioCandidate) -> Decimal:
    probabilities = (
        candidate.payout_probability, candidate.acceptance_probability, candidate.deadline_pressure,
        candidate.reputation_value, candidate.learning_value, candidate.market_concentration, candidate.payment_risk,
    )
    if any(value < 0 or value > 1 for value in probabilities):
        raise ValueError("portfolio probabilities must be between zero and one")
    if candidate.productive_minutes <= 0:
        raise ValueError("productive minutes must be positive")
    score = (
        candidate.profit_per_minute * Decimal("0.55")
        + candidate.risk_adjusted_profit * Decimal("0.01")
        + candidate.payout_probability * Decimal("0.12")
        + candidate.acceptance_probability * Decimal("0.08")
        + candidate.deadline_pressure * Decimal("0.05")
        + candidate.reputation_value * Decimal("0.03")
        + candidate.learning_value * Decimal("0.02")
        - candidate.market_concentration * Decimal("0.08")
        - candidate.payment_risk * Decimal("0.12")
    )
    return score.quantize(SIX, rounding=ROUND_HALF_UP)


def rank_portfolio_candidates(
    candidates: Iterable[PortfolioCandidate],
    *,
    available_slots: int,
    productive_minutes_available: Decimal,
) -> list[RankedCandidate]:
    scored = sorted(
        ((candidate, portfolio_score(candidate)) for candidate in candidates if candidate.expected_net_profit > 0),
        key=lambda item: (-item[1], -item[0].profit_per_minute, item[0].job_id),
    )
    remaining_minutes = max(Decimal("0"), productive_minutes_available)
    selected_count = 0
    ranked: list[RankedCandidate] = []
    for index, (candidate, score) in enumerate(scored, start=1):
        selected = (
            selected_count < max(0, int(available_slots))
            and candidate.productive_minutes <= remaining_minutes
            and candidate.risk_adjusted_profit > 0
        )
        if selected:
            selected_count += 1
            remaining_minutes -= candidate.productive_minutes
        ranked.append(RankedCandidate(candidate, score, index, selected))
    return ranked


def candidates_from_jobs(jobs: Iterable[Job]) -> list[PortfolioCandidate]:
    candidates = []
    for job in jobs:
        try:
            score = job.jobscore
        except Exception:
            continue
        payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
        decision = job.opportunity_decisions.order_by("-created_at").first()
        risk_adjusted = Decimal(str(decision.risk_adjusted_profit if decision else score.expected_profit))
        source_type = str(payload.get("source_type") or "POSTED_OPPORTUNITY")
        channel = str(payload.get("revenue_channel") or ("BOUNTY" if "bounty" in job.task_class.casefold() else "POSTED_JOB")).upper()
        if channel not in REVENUE_CHANNEL_VALUES:
            channel = "POSTED_JOB"
        candidates.append(PortfolioCandidate(
            job_id=str(job.id),
            source_type=source_type,
            revenue_channel=channel,
            expected_net_profit=Decimal(str(score.expected_profit)),
            risk_adjusted_profit=risk_adjusted,
            productive_minutes=Decimal(max(1, score.expected_minutes)),
            payout_probability=_probability(score.p_payment),
            acceptance_probability=_probability(score.p_accept),
            deadline_pressure=_probability(payload.get("deadline_pressure")),
            reputation_value=_probability(payload.get("estimated_reputation_value")),
            learning_value=_probability(payload.get("estimated_learning_value")),
            market_concentration=_probability(payload.get("market_concentration")),
            payment_risk=_probability(Decimal("1") - Decimal(str(score.p_payment))),
        ))
    return candidates


@transaction.atomic
def persist_portfolio_ranking(
    jobs: Iterable[Job],
    *,
    available_slots: int,
    productive_minutes_available: Decimal,
) -> list[RankedCandidate]:
    jobs = list(jobs)
    by_id = {str(job.id): job for job in jobs}
    ranked = rank_portfolio_candidates(
        candidates_from_jobs(jobs),
        available_slots=available_slots,
        productive_minutes_available=productive_minutes_available,
    )
    for row in ranked:
        job = by_id[row.candidate.job_id]
        try:
            inbound = job.inbound_order
        except Exception:
            inbound = None
        PortfolioDecision.objects.create(
            job=job,
            inbound_order=inbound,
            source_type=row.candidate.source_type,
            revenue_channel=row.candidate.revenue_channel,
            rank=row.rank,
            score=row.score,
            selected=row.selected,
            expected_net_profit=row.candidate.expected_net_profit,
            risk_adjusted_profit=row.candidate.risk_adjusted_profit,
            profit_per_minute=row.candidate.profit_per_minute,
            payout_probability=row.candidate.payout_probability,
            acceptance_probability=row.candidate.acceptance_probability,
            inputs={
                "productive_minutes": str(row.candidate.productive_minutes),
                "deadline_pressure": str(row.candidate.deadline_pressure),
                "reputation_value": str(row.candidate.reputation_value),
                "learning_value": str(row.candidate.learning_value),
                "market_concentration": str(row.candidate.market_concentration),
                "payment_risk": str(row.candidate.payment_risk),
                "targets_are_caps": False,
            },
        )
    AuditEvent.objects.create(
        event_type="portfolio.global_ranking_computed",
        actor="profit-brain",
        metadata={
            "candidate_count": len(ranked),
            "selected_job_ids": [row.candidate.job_id for row in ranked if row.selected],
            "channels": sorted({row.candidate.revenue_channel for row in ranked}),
        },
    )
    return ranked


def idle_capacity_actions(*, enabled_market_slugs: Iterable[str], offerings: Iterable[ServiceOffering]) -> tuple[dict, ...]:
    sellable = [offering.slug for offering in offerings if offering.proof_state == ServiceOffering.ProofState.SELLABLE and offering.enabled]
    return (
        {"action": "RATE_LIMITED_DISCOVERY", "markets": sorted(set(enabled_market_slugs)), "paid_execution": False},
        {"action": "REFRESH_SELLER_LISTING_STATUS", "paid_execution": False},
        {"action": "REPRICE_WITHIN_PROFIT_FLOOR", "paid_execution": False},
        {"action": "EXPOSE_PROVEN_CAPABILITIES", "offerings": sellable, "auto_publish": False, "paid_execution": False},
        {"action": "PROCESS_AUTHENTICATED_INBOUND_ORDERS", "auto_accept": False, "paid_execution": False},
        {"action": "REFRESH_STALE_POLICY_EVIDENCE_WHEN_ALLOWED", "external_mutation": False, "paid_execution": False},
    )
