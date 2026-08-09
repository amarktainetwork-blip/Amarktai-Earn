from __future__ import annotations

import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from control.models import (
    AcquisitionPreflight,
    Alert,
    Application,
    Bid,
    CapacitySnapshot,
    Claim,
    Execution,
    GenXCall,
    GrowthEvaluation,
    GrowthTarget,
    Job,
    OpportunityDecision,
    PerformanceAggregate,
    Payout,
    PricingStrategy,
    QAResult,
    ReputationSnapshot,
    Revision,
    StrategyAdjustment,
    Submission,
)


ZERO = Decimal("0")
CENT = Decimal("0.01")


class GrowthStage(StrEnum):
    BOOTSTRAP = "BOOTSTRAP"
    ESTABLISH = "ESTABLISH"
    PROFIT = "PROFIT"
    SCALE = "SCALE"


class UtilizationState(StrEnum):
    BUSY = "BUSY"
    PARTIALLY_IDLE = "PARTIALLY_IDLE"
    MOSTLY_IDLE = "MOSTLY_IDLE"
    IDLE = "IDLE"


class TargetStatus(StrEnum):
    AHEAD = "AHEAD"
    ON_TRACK = "ON_TRACK"
    BEHIND = "BEHIND"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class EconomicDecision:
    allowed: bool
    reason_codes: tuple[str, ...]
    growth_stage: GrowthStage
    utilization_state: UtilizationState
    expected_cash_profit: Decimal
    risk_adjusted_profit: Decimal
    reputation_contribution: Decimal
    learning_contribution: Decimal
    opportunity_cost: Decimal
    resource_minutes: Decimal
    exploration: bool
    reputation_investment: bool


@dataclass(frozen=True)
class PriceRecommendation:
    minimum_profitable_price: Decimal
    offered_price: Decimal
    desired_margin: Decimal
    adjustment_fraction: Decimal
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class SettledProfitTruth:
    settled_cash: Decimal
    paid_execution_cost: Decimal
    net_settled_profit: Decimal
    settled_payouts: int
    costed_genx_calls: int
    coverage: tuple[str, ...]


DEFAULT_TARGETS = {
    "TARGET_DAILY_SETTLED_PROFIT": ("DAILY", "10.00", "USD"),
    "TARGET_WEEKLY_SETTLED_PROFIT": ("WEEKLY", "50.00", "USD"),
    "TARGET_COMPLETED_JOBS_DAY": ("DAILY", "1", "jobs"),
    "TARGET_MIN_QA_PASS_RATE": ("ROLLING_30D", "0.90", "ratio"),
    "TARGET_MAX_REVISION_RATE": ("ROLLING_30D", "0.20", "ratio"),
    "TARGET_MAX_GENX_COST_RATIO": ("ROLLING_30D", "0.25", "ratio"),
    "TARGET_MIN_ACTIVE_MARKETS": ("ROLLING_30D", "1", "markets"),
    "TARGET_MIN_PROFITABLE_CAPABILITIES": ("ROLLING_30D", "1", "capabilities"),
}


def _decimal(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _env_decimal(name: str, default: str) -> Decimal:
    return _decimal(os.getenv(name, default), default)


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def settled_profit_truth(*, start, end=None) -> SettledProfitTruth:
    """Return USD settled cash less attributable, finalized paid execution cost in the same window."""
    payouts = Payout.objects.filter(
        state=Payout.State.SETTLED,
        currency="USD",
        settled_at__isnull=False,
        settled_at__gte=start,
    )
    if end is not None:
        payouts = payouts.filter(settled_at__lt=end)
    settled_cash = payouts.aggregate(value=Sum("net"))["value"] or ZERO
    settled_job_ids = payouts.values("job_id")
    genx_calls = GenXCall.objects.filter(
        job_id__in=settled_job_ids,
        status="COMPLETED",
        completed_at__isnull=False,
        completed_at__gte=start,
        cost_equivalent__gt=0,
    )
    if end is not None:
        genx_calls = genx_calls.filter(completed_at__lt=end)
    paid_execution_cost = genx_calls.aggregate(value=Sum("cost_equivalent"))["value"] or ZERO
    return SettledProfitTruth(
        settled_cash=_money(settled_cash),
        paid_execution_cost=_money(paid_execution_cost),
        net_settled_profit=_money(settled_cash - paid_execution_cost),
        settled_payouts=payouts.count(),
        costed_genx_calls=genx_calls.count(),
        coverage=(
            "SETTLED_PAYOUT_NET_USD_ALREADY_EXCLUDES_MARKETPLACE_FEE",
            "COMPLETED_GENX_COST_EQUIVALENT_USD_IN_WINDOW_FOR_SETTLED_JOBS",
            "NO_PERSISTED_ACTUAL_EXTERNAL_OR_OTHER_DIRECT_COST_SOURCE",
        ),
    )


def ensure_growth_targets() -> list[GrowthTarget]:
    rows = []
    for key, (period, default, unit) in DEFAULT_TARGETS.items():
        target, _ = GrowthTarget.objects.get_or_create(
            key=key,
            defaults={
                "period": period,
                "target_value": _env_decimal(key, default),
                "unit": unit,
                "details": {
                    "source": "repository_default_or_initial_environment",
                    "semantics": "OBJECTIVE_FLOOR_NEVER_EARNINGS_CAP",
                },
            },
        )
        if target.details.get("semantics") != "OBJECTIVE_FLOOR_NEVER_EARNINGS_CAP":
            target.details = {**target.details, "semantics": "OBJECTIVE_FLOOR_NEVER_EARNINGS_CAP"}
            target.save(update_fields=["details", "updated_at"])
        rows.append(target)
    return rows


def classify_growth_stage(*, sample_count: int, completed_jobs: int, settled_profit: Decimal, qa_rate: Decimal, settlement_rate: Decimal) -> GrowthStage:
    if sample_count < 5 or completed_jobs < 3:
        return GrowthStage.BOOTSTRAP
    if sample_count < 20 or completed_jobs < 10:
        return GrowthStage.ESTABLISH
    if sample_count >= 50 and completed_jobs >= 25 and settled_profit > 0 and qa_rate >= Decimal("0.90") and settlement_rate >= Decimal("0.80"):
        return GrowthStage.SCALE
    return GrowthStage.PROFIT


def classify_utilization(active_slots: int, productive_slots: int) -> tuple[UtilizationState, Decimal]:
    if productive_slots <= 0:
        return UtilizationState.BUSY, Decimal("1")
    ratio = (Decimal(active_slots) / Decimal(productive_slots)).quantize(Decimal("0.000001"))
    if ratio >= Decimal("0.80"):
        return UtilizationState.BUSY, ratio
    if ratio >= Decimal("0.45"):
        return UtilizationState.PARTIALLY_IDLE, ratio
    if ratio >= Decimal("0.15"):
        return UtilizationState.MOSTLY_IDLE, ratio
    return UtilizationState.IDLE, ratio


def current_growth_stage(marketplace, capability: str) -> GrowthStage:
    row = PerformanceAggregate.objects.filter(
        dimension_type="MARKET_CAPABILITY",
        dimension_key=f"{marketplace.slug}:{capability}",
    ).order_by("-window_end", "-created_at").first()
    if not row:
        return GrowthStage.BOOTSTRAP
    try:
        return GrowthStage(row.growth_stage)
    except ValueError:
        return GrowthStage.BOOTSTRAP


def capture_capacity(*, persist: bool = True, node_id: str | None = None) -> CapacitySnapshot:
    slots = max(1, int(os.getenv("AMARKTAI_PRODUCTIVE_CAPACITY_SLOTS", os.getenv("MAX_ACTIVE_JOBS", "4"))))
    active = Execution.objects.filter(status__in=["QUEUED", "EXECUTING", "REPAIRING"]).count()
    reserved = Job.objects.filter(state__in=[Job.State.CLAIMED, Job.State.AWARDED]).count()
    reserved = min(max(0, slots - active), reserved)
    available = max(0, slots - active - reserved)
    state, utilization = classify_utilization(active + reserved, slots)
    latest_by_job = {}
    preflights = AcquisitionPreflight.objects.filter(
        job__state=Job.State.EXPECTED,
        job__jobscore__expected_profit__gt=0,
    ).order_by("job_id", "-created_at")
    for preflight in preflights:
        latest_by_job.setdefault(preflight.job_id, preflight)
    eligible_ids = [job_id for job_id, preflight in latest_by_job.items() if preflight.eligible]
    waiting_jobs = Job.objects.filter(id__in=eligible_ids).select_related("jobscore")
    waiting = waiting_jobs.count()
    interval = max(1, int(os.getenv("CAPACITY_SNAPSHOT_INTERVAL_MINUTES", "5")))
    avoidable = Decimal(available * interval if waiting and available else 0)
    unavoidable = Decimal(available * interval if not waiting else 0)
    foregone = sum(
        (job.jobscore.expected_profit for job in waiting_jobs.order_by("-jobscore__expected_profit_per_minute")[:available]),
        ZERO,
    )
    idle_reason = ""
    if available and waiting:
        idle_reason = "ELIGIBLE_PROFITABLE_WORK_WAITING"
    elif available:
        idle_reason = "NO_ELIGIBLE_PROFITABLE_WORK"
    elif reserved:
        idle_reason = "CAPACITY_RESERVED_FOR_AWARDED_WORK"
    else:
        idle_reason = "CAPACITY_SATURATED"
    values = {
        "node_id": node_id or os.getenv("NODE_ID", "VPS1"),
        "productive_slots": slots,
        "active_slots": active,
        "available_slots": available,
        "reserved_slots": reserved,
        "utilization": utilization,
        "utilization_state": state.value,
        "profitable_eligible_waiting": waiting,
        "avoidable_idle_minutes": avoidable,
        "unavoidable_idle_minutes": unavoidable,
        "estimated_foregone_profit": _money(foregone),
        "idle_reason": idle_reason,
        "details": {"snapshot_interval_minutes": interval},
    }
    snapshot = CapacitySnapshot.objects.create(**values) if persist else CapacitySnapshot(**values)
    if persist and avoidable > 0:
        if not Alert.objects.filter(alert_type="AVOIDABLE_IDLE_PROFITABLE_WORK", status="OPEN").exists():
            Alert.objects.create(
                alert_type="AVOIDABLE_IDLE_PROFITABLE_WORK",
                status="OPEN",
                severity="WARNING",
                message="Safe productive capacity is idle while eligible positive-expected-profit work is waiting.",
                metadata={"capacity_snapshot_id": snapshot.id, "waiting": waiting, "available_slots": available},
            )
    return snapshot


def _minimum_profit_rate(state: UtilizationState) -> Decimal:
    defaults = {
        UtilizationState.BUSY: ("PROFIT_RATE_BUSY_USD", "0.25"),
        UtilizationState.PARTIALLY_IDLE: ("PROFIT_RATE_PARTIALLY_IDLE_USD", "0.10"),
        UtilizationState.MOSTLY_IDLE: ("PROFIT_RATE_MOSTLY_IDLE_USD", "0.02"),
        UtilizationState.IDLE: ("PROFIT_RATE_IDLE_USD", "0.00"),
    }
    name, default = defaults[state]
    return _env_decimal(name, default)


def discovery_limit(base_limit: int, state: UtilizationState) -> int:
    multiplier = {
        UtilizationState.BUSY: Decimal("0.50"),
        UtilizationState.PARTIALLY_IDLE: Decimal("1.00"),
        UtilizationState.MOSTLY_IDLE: Decimal("1.50"),
        UtilizationState.IDLE: Decimal("2.00"),
    }[state]
    return max(1, min(500, int(Decimal(max(1, int(base_limit))) * multiplier)))


def _exploration_allowed(job, capability: str) -> tuple[bool, bool]:
    has_evidence = PerformanceAggregate.objects.filter(
        dimension_type="MARKET_CAPABILITY",
        dimension_key=f"{job.marketplace.slug}:{capability}",
        sample_count__gte=5,
    ).exists()
    if has_evidence:
        return False, True
    if job.opportunity_decisions.filter(exploration=True, allowed=True).exists():
        return True, True
    fraction = min(Decimal("0.50"), max(ZERO, _env_decimal("EXPLORATION_CAPACITY_FRACTION", "0.10")))
    since = timezone.now() - timedelta(days=1)
    total = OpportunityDecision.objects.filter(created_at__gte=since).count()
    explored = OpportunityDecision.objects.filter(created_at__gte=since, exploration=True, allowed=True).count()
    allowance = max(1, math.ceil(max(1, total) * float(fraction)))
    return True, explored < allowance


def evaluate_opportunity(job, *, capacity: CapacitySnapshot | None = None, capability: str = "") -> EconomicDecision:
    score = job.jobscore
    capability = capability or str(job.normalized_payload.get("operation") or job.task_class).strip().casefold()
    stage = current_growth_stage(job.marketplace, capability)
    capacity = capacity or capture_capacity(persist=False)
    utilization = UtilizationState(capacity.utilization_state)
    expected_profit = _decimal(score.expected_profit)
    risk_adjusted = expected_profit
    resource_minutes = _decimal(score.expected_minutes, "1")
    ppm = _decimal(score.expected_profit_per_minute)
    payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    reputation = _decimal(payload.get("estimated_reputation_value"), "0")
    learning = _decimal(payload.get("estimated_learning_value"), "0")
    reasons: list[str] = []
    opportunity_cost = ZERO

    if capacity.available_slots <= 0 or utilization == UtilizationState.BUSY:
        competing = Job.objects.filter(
            state__in=[Job.State.CLAIMED, Job.State.AWARDED, Job.State.EXECUTING],
            jobscore__isnull=False,
        ).exclude(pk=job.pk).order_by("-jobscore__expected_profit_per_minute").select_related("jobscore").first()
        if competing and competing.jobscore.expected_profit_per_minute > ppm:
            opportunity_cost = _money((competing.jobscore.expected_profit_per_minute - ppm) * resource_minutes)
            reasons.append("BETTER_COMMITTED_WORK_HAS_PRIORITY")

    reputation_investment = False
    if expected_profit <= 0:
        enabled = os.getenv("REPUTATION_INVESTMENT_ENABLED", "0") == "1"
        limit = max(ZERO, _env_decimal("REPUTATION_INVESTMENT_DAILY_LIMIT", "0"))
        spent = OpportunityDecision.objects.filter(
            created_at__date=timezone.localdate(), reputation_investment=True, allowed=True,
        ).aggregate(total=Sum("expected_cash_profit"))["total"] or ZERO
        spent = abs(min(ZERO, spent))
        proposed_loss = abs(min(ZERO, expected_profit))
        if enabled and limit > 0 and stage == GrowthStage.BOOTSTRAP and reputation > 0 and spent + proposed_loss <= limit:
            reputation_investment = True
        else:
            reasons.append("NON_POSITIVE_EXPECTED_PROFIT")

    if not reputation_investment and ppm < _minimum_profit_rate(utilization):
        reasons.append("UTILIZATION_ADJUSTED_MARGIN_TOO_LOW")
    exploration, exploration_available = _exploration_allowed(job, capability)
    if exploration and not exploration_available:
        reasons.append("EXPLORATION_ALLOCATION_EXHAUSTED")
    if opportunity_cost > 0:
        risk_adjusted -= opportunity_cost
    if risk_adjusted <= 0 and not reputation_investment:
        reasons.append("RISK_ADJUSTED_PROFIT_NOT_POSITIVE")
    return EconomicDecision(
        allowed=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
        growth_stage=stage,
        utilization_state=utilization,
        expected_cash_profit=_money(expected_profit),
        risk_adjusted_profit=_money(risk_adjusted),
        reputation_contribution=reputation,
        learning_contribution=learning,
        opportunity_cost=_money(opportunity_cost),
        resource_minutes=resource_minutes,
        exploration=exploration,
        reputation_investment=reputation_investment,
    )


def persist_opportunity_decision(job, decision: EconomicDecision, *, capacity=None, preflight=None, pricing_strategy=None, allowed: bool | None = None, reason_codes=None) -> OpportunityDecision:
    return OpportunityDecision.objects.create(
        job=job,
        preflight=preflight,
        capacity=capacity,
        pricing_strategy=pricing_strategy,
        growth_stage=decision.growth_stage.value,
        utilization_state=decision.utilization_state.value,
        allowed=decision.allowed if allowed is None else allowed,
        exploration=decision.exploration,
        reputation_investment=decision.reputation_investment,
        expected_cash_profit=decision.expected_cash_profit,
        risk_adjusted_profit=decision.risk_adjusted_profit,
        reputation_contribution=decision.reputation_contribution,
        learning_contribution=decision.learning_contribution,
        opportunity_cost=decision.opportunity_cost,
        resource_minutes=decision.resource_minutes,
        reason_codes=list(decision.reason_codes if reason_codes is None else reason_codes),
        details={"score_version": job.jobscore.score_version},
    )


def recommend_price(*, total_expected_cost: Decimal, advertised_budget: Decimal | None, competitive_price: Decimal | None, fee_rate: Decimal, utilization_state: UtilizationState, growth_stage: GrowthStage, historical_win_rate: Decimal | None = None) -> PriceRecommendation:
    if total_expected_cost < 0 or fee_rate < 0 or fee_rate >= 1:
        raise ValueError("invalid pricing inputs")
    margin_defaults = {
        GrowthStage.BOOTSTRAP: Decimal("0.05"),
        GrowthStage.ESTABLISH: Decimal("0.10"),
        GrowthStage.PROFIT: Decimal("0.20"),
        GrowthStage.SCALE: Decimal("0.25"),
    }
    desired_margin = margin_defaults[growth_stage]
    if utilization_state == UtilizationState.BUSY:
        desired_margin += Decimal("0.10")
    elif utilization_state == UtilizationState.IDLE:
        desired_margin = max(Decimal("0.01"), desired_margin - Decimal("0.05"))
    minimum = _money(total_expected_cost / (Decimal("1") - fee_rate))
    base = competitive_price or advertised_budget or minimum
    adjustment = ZERO
    reasons = []
    max_adjustment = min(Decimal("0.20"), max(ZERO, _env_decimal("PRICING_MAX_ADJUSTMENT_FRACTION", "0.10")))
    if historical_win_rate is not None and historical_win_rate >= Decimal("0.80"):
        adjustment = max_adjustment
        reasons.append("HISTORICAL_WIN_RATE_SUGGESTS_PRICE_TOO_LOW")
    elif historical_win_rate is not None and historical_win_rate <= Decimal("0.20"):
        adjustment = -max_adjustment
        reasons.append("HISTORICAL_WIN_RATE_SUGGESTS_BOUNDED_PRICE_TEST")
    target = base * (Decimal("1") + adjustment)
    cost_plus_margin = minimum * (Decimal("1") + desired_margin)
    offer = max(minimum, cost_plus_margin, target)
    if advertised_budget is not None:
        offer = min(offer, advertised_budget)
        if advertised_budget < minimum:
            reasons.append("ADVERTISED_BUDGET_BELOW_MINIMUM_PROFITABLE_PRICE")
            offer = minimum
    return PriceRecommendation(minimum, _money(offer), desired_margin, adjustment, tuple(reasons))


def persist_pricing_strategy(job, *, capability: str, operation: str, recommendation: PriceRecommendation, capacity: CapacitySnapshot, stage: GrowthStage, advertised_budget=None, competitive_price=None, exploration=False) -> PricingStrategy:
    return PricingStrategy.objects.create(
        marketplace=job.marketplace,
        capability=capability,
        operation=operation,
        growth_stage=stage.value,
        utilization_state=capacity.utilization_state,
        minimum_profitable_price=recommendation.minimum_profitable_price,
        advertised_budget=advertised_budget,
        competitive_price=competitive_price,
        offered_price=recommendation.offered_price,
        desired_margin=recommendation.desired_margin,
        exploration=exploration,
        adjustment_fraction=recommendation.adjustment_fraction,
        reason_codes=list(recommendation.reason_codes),
    )


def record_reputation_snapshot(*, marketplace, source: str, rating=None, rating_count: int = 0, completed_jobs: int = 0, capability: str = "", revision_rate=ZERO, on_time_rate=ZERO, observed_at=None, details=None) -> ReputationSnapshot:
    if not source.strip():
        raise ValueError("reputation source is required; values must not be invented")
    parsed_rating = None if rating in (None, "") else _decimal(rating)
    if parsed_rating is not None and parsed_rating < 0:
        raise ValueError("reputation rating cannot be negative")
    return ReputationSnapshot.objects.create(
        marketplace=marketplace,
        capability=capability,
        rating=parsed_rating,
        rating_count=max(0, int(rating_count)),
        completed_jobs=max(0, int(completed_jobs)),
        revision_rate=max(ZERO, _decimal(revision_rate)),
        on_time_rate=max(ZERO, _decimal(on_time_rate)),
        source=source.strip(),
        observed_at=observed_at or timezone.now(),
        details=details or {},
    )


def _ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    if not denominator:
        return ZERO
    return (_decimal(numerator) / _decimal(denominator)).quantize(Decimal("0.000001"))


def _average_seconds(values) -> int:
    cleaned = [max(0, int(value)) for value in values if value is not None]
    return 0 if not cleaned else sum(cleaned) // len(cleaned)


def _job_dimensions(job) -> list[tuple[str, str, dict]]:
    dimensions = [("MARKET", job.marketplace.slug, {"marketplace": job.marketplace})]
    execution = job.executions.select_related("worker").order_by("attempt").first()
    if execution and execution.worker:
        operation = str(execution.result.get("operation") or "")
        worker = execution.worker
        dimensions.extend([
            ("CAPABILITY", worker.worker_class, {"capability": worker.worker_class}),
            ("OPERATION", operation or worker.worker_class, {"operation": operation}),
            ("WORKER", f"{worker.worker_class}:{worker.version}", {"worker_class": worker.worker_class, "worker_version": worker.version}),
            ("MARKET_CAPABILITY", f"{job.marketplace.slug}:{worker.worker_class}", {"marketplace": job.marketplace, "capability": worker.worker_class}),
        ])
    strategy = job.opportunity_decisions.select_related("pricing_strategy").exclude(pricing_strategy=None).order_by("created_at").first()
    if strategy and strategy.pricing_strategy:
        dimensions.append(("PRICING", str(strategy.pricing_strategy_id), {"strategy_key": str(strategy.pricing_strategy_id)}))
    return dimensions


def refresh_performance(*, window_days: int = 30) -> list[PerformanceAggregate]:
    end = timezone.now()
    start = end - timedelta(days=max(1, min(int(window_days), 365)))
    jobs = list(
        Job.objects.filter(created_at__gte=start).select_related("marketplace", "jobscore").prefetch_related(
            "executions__worker", "applications", "bids", "claims", "artifacts", "opportunity_decisions__pricing_strategy"
        )
    )
    groups: dict[tuple[str, str], dict] = defaultdict(lambda: {"jobs": [], "defaults": {}})
    for job in jobs:
        for dimension_type, dimension_key, defaults in _job_dimensions(job):
            groups[(dimension_type, dimension_key)]["jobs"].append(job)
            groups[(dimension_type, dimension_key)]["defaults"] = defaults
    rows = []
    for (dimension_type, dimension_key), group in groups.items():
        selected = group["jobs"]
        ids = [job.id for job in selected]
        executions = list(Execution.objects.filter(job_id__in=ids))
        qa = list(QAResult.objects.filter(job_id__in=ids).order_by("created_at"))
        payouts = list(Payout.objects.filter(job_id__in=ids))
        settled_payouts = list(
            Payout.objects.filter(
                job_id__in=ids,
                state=Payout.State.SETTLED,
                currency="USD",
                settled_at__gte=start,
                settled_at__lt=end,
            )
        )
        settled_job_ids = [payout.job_id for payout in settled_payouts]
        genx = list(
            GenXCall.objects.filter(
                job_id__in=settled_job_ids,
                status="COMPLETED",
                completed_at__isnull=False,
                completed_at__gte=start,
                completed_at__lt=end,
            )
        )
        revisions = Revision.objects.filter(job_id__in=ids).count()
        submissions = list(Submission.objects.filter(job_id__in=ids))
        first_qa = {}
        for result in qa:
            first_qa.setdefault(result.job_id, result)
        applications = list(Application.objects.filter(job_id__in=ids))
        bids = list(Bid.objects.filter(job_id__in=ids))
        claims = list(Claim.objects.filter(job_id__in=ids))
        attempts = [*applications, *bids, *claims]
        attempted = len(attempts)
        awarded = sum(1 for job in selected if job.state in {Job.State.CLAIMED, Job.State.AWARDED, Job.State.EXECUTING, Job.State.SUBMITTED, Job.State.ACCEPTED, Job.State.PAYOUT_PENDING, Job.State.SETTLED} or job.executions.exists())
        completed_ids = {result.job_id for result in qa if result.passed}
        completed = len(completed_ids)
        first_pass = sum(1 for result in first_qa.values() if result.passed)
        repaired = len({execution.job_id for execution in executions if execution.attempt > 1})
        accepted = sum(1 for job in selected if job.state in {Job.State.ACCEPTED, Job.State.PAYOUT_PENDING, Job.State.SETTLED})
        settled = len(settled_payouts)
        on_time = 0
        for job in selected:
            first_submission = next((row for row in submissions if row.job_id == job.id), None)
            if first_submission and (not job.deadline or first_submission.created_at <= job.deadline):
                on_time += 1
        gross = sum((payout.gross for payout in settled_payouts), ZERO)
        fees = sum((payout.fee for payout in settled_payouts), ZERO)
        genx_cost = sum((call.cost_equivalent for call in genx), ZERO)
        expected_cost = sum((_decimal(job.jobscore.expected_genx_cost) + _decimal(job.jobscore.expected_external_cost) for job in selected if hasattr(job, "jobscore")), ZERO)
        actual_cost = genx_cost
        settled_cash = sum((payout.net for payout in settled_payouts), ZERO)
        profit = settled_cash - actual_cost
        runtime_seconds = sum(
            (max(0, int((execution.ended_at - execution.started_at).total_seconds())) for execution in executions if execution.started_at and execution.ended_at),
            0,
        )
        credits = sum((call.credits for call in genx), ZERO)
        award_seconds = [
            (attempt.updated_at - attempt.created_at).total_seconds()
            for attempt in attempts
            if attempt.status in {"FUNDED", "AWARDED", "ACCEPTED", "CLAIMED"}
        ]
        submission_by_job = {}
        for submission in submissions:
            submission_by_job.setdefault(submission.job_id, submission)
        acceptance_seconds = []
        settlement_seconds = []
        for payout in payouts:
            submission = submission_by_job.get(payout.job_id)
            if submission and payout.earned_at:
                acceptance_seconds.append((payout.earned_at - submission.created_at).total_seconds())
            if payout.settled_at and (payout.earned_at or payout.pending_at):
                settlement_seconds.append((payout.settled_at - (payout.earned_at or payout.pending_at)).total_seconds())
        reputation_rows = ReputationSnapshot.objects.filter(
            marketplace_id__in={job.marketplace_id for job in selected},
            observed_at__gte=start,
            observed_at__lte=end,
        ).exclude(rating=None).order_by("observed_at")
        if group["defaults"].get("capability"):
            reputation_rows = reputation_rows.filter(Q(capability=group["defaults"]["capability"]) | Q(capability=""))
        reputation_values = list(reputation_rows.values_list("rating", flat=True))
        reputation_delta = None if len(reputation_values) < 2 else reputation_values[-1] - reputation_values[0]
        qa_rate = _ratio(first_pass, completed or len(first_qa))
        settlement_rate = _ratio(settled, accepted or completed)
        stage = classify_growth_stage(
            sample_count=len(selected), completed_jobs=completed, settled_profit=profit,
            qa_rate=qa_rate, settlement_rate=settlement_rate,
        )
        row = PerformanceAggregate.objects.create(
            dimension_type=dimension_type,
            dimension_key=dimension_key,
            window_start=start,
            window_end=end,
            jobs_discovered=len(selected),
            jobs_attempted=attempted,
            jobs_awarded=awarded,
            jobs_completed=completed,
            qa_first_pass_rate=qa_rate,
            repair_rate=_ratio(repaired, completed),
            revision_rate=_ratio(revisions, completed),
            on_time_rate=_ratio(on_time, completed),
            acceptance_rate=_ratio(accepted, awarded),
            settlement_rate=settlement_rate,
            gross_payout=_money(gross),
            platform_fees=_money(fees),
            genx_cost=genx_cost,
            direct_cost=ZERO,
            expected_cost=expected_cost,
            actual_cost=actual_cost,
            settled_profit=_money(profit),
            runtime_seconds=runtime_seconds,
            time_to_award_seconds=_average_seconds(award_seconds),
            time_to_acceptance_seconds=_average_seconds(acceptance_seconds),
            time_to_settlement_seconds=_average_seconds(settlement_seconds),
            profit_per_execution_minute=ZERO if runtime_seconds <= 0 else (profit / (Decimal(runtime_seconds) / Decimal(60))).quantize(Decimal("0.0001")),
            profit_per_genx_credit=None if credits <= 0 else (profit / credits).quantize(Decimal("0.0001")),
            reputation_delta=reputation_delta,
            sample_count=len(selected),
            growth_stage=stage.value,
            details={
                "window_days": window_days,
                "settled_profit_formula": "settled_payout_net_usd_minus_completed_genx_cost_equivalent_usd",
                "marketplace_fee_handling": "payout_net_already_excludes_fee; fee_not_subtracted_again",
                "actual_external_direct_cost_coverage": "NO_PERSISTED_SOURCE",
            },
            **group["defaults"],
        )
        rows.append(row)
    return rows


def _growth_metrics(now) -> dict[str, Decimal]:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    settled = Payout.objects.filter(state=Payout.State.SETTLED, currency="USD")
    daily_truth = settled_profit_truth(start=day_start, end=now + timedelta(microseconds=1))
    weekly_truth = settled_profit_truth(start=week_start, end=now + timedelta(microseconds=1))
    completed_day = Job.objects.filter(updated_at__gte=day_start, state__in=[Job.State.SUBMITTED, Job.State.ACCEPTED, Job.State.PAYOUT_PENDING, Job.State.SETTLED]).count()
    since = now - timedelta(days=30)
    qa = QAResult.objects.filter(created_at__gte=since)
    completed = qa.values("job_id").distinct().count()
    revisions = Revision.objects.filter(created_at__gte=since).count()
    gross = settled.filter(settled_at__gte=since).aggregate(v=Sum("gross"))["v"] or ZERO
    genx_cost = GenXCall.objects.filter(
        status="COMPLETED",
        completed_at__gte=since,
    ).aggregate(v=Sum("cost_equivalent"))["v"] or ZERO
    active_markets = Job.objects.filter(
        created_at__gte=since,
        marketplace__enabled=True,
        marketplace__status="LIVE",
    ).values("marketplace_id").distinct().count()
    profitable_capabilities = PerformanceAggregate.objects.filter(window_end__gte=since, dimension_type="CAPABILITY", settled_profit__gt=0).values("dimension_key").distinct().count()
    return {
        "TARGET_DAILY_SETTLED_PROFIT": _decimal(daily_truth.net_settled_profit),
        "TARGET_WEEKLY_SETTLED_PROFIT": _decimal(weekly_truth.net_settled_profit),
        "TARGET_COMPLETED_JOBS_DAY": Decimal(completed_day),
        "TARGET_MIN_QA_PASS_RATE": _ratio(qa.filter(passed=True).count(), qa.count()),
        "TARGET_MAX_REVISION_RATE": _ratio(revisions, completed),
        "TARGET_MAX_GENX_COST_RATIO": ZERO if gross <= 0 else (genx_cost / gross).quantize(Decimal("0.000001")),
        "TARGET_MIN_ACTIVE_MARKETS": Decimal(active_markets),
        "TARGET_MIN_PROFITABLE_CAPABILITIES": Decimal(profitable_capabilities),
        "ACTUAL_DAILY_SETTLED_CASH": daily_truth.settled_cash,
        "ACTUAL_DAILY_PAID_EXECUTION_COST": daily_truth.paid_execution_cost,
        "ACTUAL_DAILY_NET_SETTLED_PROFIT": daily_truth.net_settled_profit,
        "ACTUAL_WEEKLY_SETTLED_CASH": weekly_truth.settled_cash,
        "ACTUAL_WEEKLY_PAID_EXECUTION_COST": weekly_truth.paid_execution_cost,
        "ACTUAL_WEEKLY_NET_SETTLED_PROFIT": weekly_truth.net_settled_profit,
    }


def evaluate_growth_targets(*, persist: bool = True) -> GrowthEvaluation:
    targets = {row.key: row for row in ensure_growth_targets() if row.enabled}
    now = timezone.now()
    metrics = _growth_metrics(now)
    reasons = []
    ratios = []
    for key, target in targets.items():
        actual = metrics.get(key, ZERO)
        desired = target.target_value
        maximum = key.startswith("TARGET_MAX_")
        if maximum:
            if actual > desired:
                reasons.append(key.removeprefix("TARGET_MAX_") + "_HIGH")
            ratios.append(Decimal("1") if actual <= desired else (desired / actual if actual else Decimal("1")))
        else:
            if actual < desired:
                reasons.append(key.removeprefix("TARGET_") + "_BEHIND")
            ratios.append(Decimal("1") if desired <= 0 else actual / desired)
    if not Job.objects.exists():
        status = TargetStatus.INSUFFICIENT_DATA
        reasons = ["INSUFFICIENT_OPPORTUNITIES"]
    else:
        floor = min(ratios or [ZERO])
        status = TargetStatus.AHEAD if floor >= Decimal("1.20") else TargetStatus.ON_TRACK if floor >= Decimal("1") else TargetStatus.BEHIND
        latest_capacity = CapacitySnapshot.objects.order_by("-created_at").first()
        if latest_capacity and latest_capacity.avoidable_idle_minutes > 0:
            reasons.append("CAPACITY_IDLE")
        elif latest_capacity and latest_capacity.available_slots == 0:
            reasons.append("CAPACITY_SATURATED")
    adjustments = []
    for reason in list(dict.fromkeys(reasons)):
        mapping = {
            "CAPACITY_IDLE": "INCREASE_APPROVED_DISCOVERY_INTENSITY",
            "CAPACITY_SATURATED": "RAISE_MARGIN_THRESHOLD_AND_PROTECT_COMMITMENTS",
            "GENX_COST_RATIO_HIGH": "PREFER_EFFICIENT_PROVEN_MODELS",
            "REVISION_RATE_HIGH": "RAISE_QA_AND_REPAIR_PRIORITY",
        }
        if reason in mapping:
            adjustments.append(mapping[reason])
            if persist:
                StrategyAdjustment.objects.create(
                    scope_type="GLOBAL", scope_key="profit-engine", growth_stage=GrowthStage.BOOTSTRAP.value,
                    adjustment_type=mapping[reason], reason_codes=[reason], bounded=True, applied=False,
                )
    values = {
        "status": status.value,
        "window_start": now - timedelta(days=30),
        "window_end": now,
        "reason_codes": list(dict.fromkeys(reasons)),
        "metrics": {key: str(value) for key, value in metrics.items()},
        "targets": {key: str(row.target_value) for key, row in targets.items()},
        "adjustments": adjustments,
    }
    return GrowthEvaluation.objects.create(**values) if persist else GrowthEvaluation(**values)


@transaction.atomic
def refresh_profit_intelligence() -> dict:
    capacity = capture_capacity(persist=True)
    performance = refresh_performance(window_days=30)
    growth = evaluate_growth_targets(persist=True)
    return {
        "capacity_snapshot_id": capacity.id,
        "performance_snapshots": len(performance),
        "growth_evaluation_id": growth.id,
        "growth_status": growth.status,
    }
