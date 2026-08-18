from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, Job, PortfolioDecision, ServiceOffering
from control.services.autonomy import AutonomyMode, current_mode
from control.services.seller_services import is_offering_currently_sellable


SIX = Decimal("0.000001")
REVENUE_CHANNEL_VALUES = {
    "POSTED_JOB", "BOUNTY", "SERVICE_LISTING", "PAY_PER_CALL_API",
    "PROJECT_HIRE", "SUBSCRIPTION", "MANUAL_STOREFRONT", "DIRECT_CHECKOUT",
    "PAYMENT_LINK", "OFFHOST_SETTLEMENT",
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
    eligible: bool = False
    action_allowed: bool = False
    selection_blockers: tuple[str, ...] = ()

    @property
    def profit_per_minute(self) -> Decimal:
        return self.risk_adjusted_profit / max(Decimal("1"), self.productive_minutes)


@dataclass(frozen=True)
class RankedCandidate:
    candidate: PortfolioCandidate
    score: Decimal
    rank: int
    selected: bool
    would_select_if_enabled: bool


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
    shadow_remaining_minutes = remaining_minutes
    selected_count = 0
    shadow_selected_count = 0
    ranked: list[RankedCandidate] = []
    for index, (candidate, score) in enumerate(scored, start=1):
        would_select_if_enabled = (
            candidate.eligible
            and shadow_selected_count < max(0, int(available_slots))
            and candidate.productive_minutes <= shadow_remaining_minutes
            and candidate.risk_adjusted_profit > 0
        )
        if would_select_if_enabled:
            shadow_selected_count += 1
            shadow_remaining_minutes -= candidate.productive_minutes
        selected = (
            candidate.eligible
            and candidate.action_allowed
            and selected_count < max(0, int(available_slots))
            and candidate.productive_minutes <= remaining_minutes
            and candidate.risk_adjusted_profit > 0
        )
        if selected:
            selected_count += 1
            remaining_minutes -= candidate.productive_minutes
        ranked.append(RankedCandidate(candidate, score, index, selected, would_select_if_enabled))
    return ranked


def _selection_truth(job: Job, source_type: str) -> tuple[bool, bool, tuple[str, ...]]:
    blockers: list[str] = []
    commercial_api = source_type == "COMMERCIAL_API"
    preflight = job.acquisition_preflights.order_by("-created_at").first()
    if preflight is None:
        if commercial_api:
            # Authenticated commercial API demand has already passed its
            # canonical quota, funding, idempotency, and margin admission.
            return True, True, ()
        return False, False, ("NO_VALID_ACQUISITION_PREFLIGHT",)
    eligible = preflight.eligible
    action_allowed = preflight.allowed
    if not preflight.eligible:
        blockers.append("PREFLIGHT_INELIGIBLE")
    if not preflight.allowed:
        blockers.append("PREFLIGHT_ACTION_NOT_ALLOWED")
    blockers.extend(str(reason) for reason in preflight.reason_codes)

    market = job.marketplace
    current_safety_blockers: list[str] = []
    if not market.enabled:
        current_safety_blockers.append("MARKET_DISABLED")
    if market.status != market.Status.LIVE:
        current_safety_blockers.append("MARKET_NOT_LIVE")
    if not market.payout_ready:
        current_safety_blockers.append("PAYOUT_NOT_READY")
    if not market.south_africa_verified:
        current_safety_blockers.append("SOUTH_AFRICA_NOT_VERIFIED")
    policy = market.policy_versions.order_by("-checked_at", "-created_at").first()
    if policy is None or not policy.automation_allowed:
        current_safety_blockers.append("MARKET_AUTOMATION_POLICY_NOT_VERIFIED")
    elif policy.checked_at < timezone.now() - timedelta(days=max(1, int(os.getenv("MARKET_POLICY_MAX_AGE_DAYS", "30")))):
        current_safety_blockers.append("MARKET_AUTOMATION_POLICY_STALE")
    if policy and not policy.webdock_compatible:
        current_safety_blockers.append("MARKET_RUNTIME_NOT_COMPATIBLE")
    try:
        profile = market.integration_profile
    except Exception:
        profile = None
    if profile is None:
        current_safety_blockers.append("MARKET_INTEGRATION_PROFILE_MISSING")
    else:
        current_safety_blockers.extend(str(reason) for reason in profile.blockers)

    inbound = source_type == "INBOUND_SERVICE_ORDER"
    current_action_blockers: list[str] = []
    if inbound:
        try:
            order = job.inbound_order
        except Exception:
            order = None
        if order is None or order.listing_id is None or order.listing.status != "PUBLISHED":
            current_safety_blockers.append("INBOUND_SERVICE_LISTING_NOT_PUBLISHED")
        if profile is None or not profile.seller_capabilities.get("receive_orders"):
            current_safety_blockers.append("RECEIVE_ORDERS_CAPABILITY_NOT_VERIFIED")
        if profile and profile.hosting_policy != "WEBDOCK_SAFE":
            current_safety_blockers.append("MARKET_NOT_WEBDOCK_SAFE")
        if os.getenv("INBOUND_SERVICE_AUTO_ACCEPT_ENABLED", "0") != "1":
            current_action_blockers.append("INBOUND_SERVICE_AUTO_ACCEPT_DISABLED")
    else:
        if profile is None or not profile.autonomous_acquisition_enabled:
            current_action_blockers.append("MARKET_AUTONOMOUS_ACQUISITION_DISABLED")
        switch_name = f"{market.slug.upper().replace('-', '_')}_AUTO_ACQUIRE_ENABLED"
        legacy_default = "1" if market.slug == "agentgigs" and os.getenv("AGENTGIGS_AUTO_APPLY_ENABLED", "0") == "1" else "0"
        if os.getenv(switch_name, legacy_default) != "1":
            current_action_blockers.append("ACQUISITION_SWITCH_DISABLED")
        if current_mode() not in {AutonomyMode.LOW_RISK, AutonomyMode.FULL}:
            current_action_blockers.append("AUTONOMY_ACTION_DISABLED")

    if current_safety_blockers:
        eligible = False
        action_allowed = False
    if current_action_blockers:
        action_allowed = False
    blockers.extend(current_safety_blockers)
    blockers.extend(current_action_blockers)
    return eligible, action_allowed, tuple(dict.fromkeys(blockers))


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
        eligible, action_allowed, selection_blockers = _selection_truth(job, source_type)
        if str(payload.get("source_classification") or "").upper() == "MARKETING_DEPENDENT":
            eligible = False
            action_allowed = False
            selection_blockers = (*selection_blockers, "MARKETING_DEPENDENT_EXCLUDED")
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
            eligible=eligible,
            action_allowed=action_allowed,
            selection_blockers=selection_blockers,
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
                "eligible": row.candidate.eligible,
                "action_allowed": row.candidate.action_allowed,
                "selection_blockers": list(row.candidate.selection_blockers),
                "shadow_rank": row.rank,
                "would_select_if_enabled": row.would_select_if_enabled,
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
    sellable = [offering.slug for offering in offerings if is_offering_currently_sellable(offering)]
    return (
        {"action": "RATE_LIMITED_DISCOVERY", "markets": sorted(set(enabled_market_slugs)), "paid_execution": False},
        {"action": "REFRESH_SELLER_LISTING_STATUS", "paid_execution": False},
        {"action": "REPRICE_WITHIN_PROFIT_FLOOR", "paid_execution": False},
        {"action": "EXPOSE_PROVEN_CAPABILITIES", "offerings": sellable, "auto_publish": False, "paid_execution": False},
        {"action": "PROCESS_AUTHENTICATED_INBOUND_ORDERS", "auto_accept": False, "paid_execution": False},
        {"action": "REFRESH_STALE_POLICY_EVIDENCE_WHEN_ALLOWED", "external_mutation": False, "paid_execution": False},
    )
