from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Any, Iterable

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from control.models import Claim, GenXCall, Job, Payout, PortfolioDecision, Submission
from control.services.autonomy import AutonomyMode, current_mode
from control.services.revenue_portfolio import PortfolioCandidate, RankedCandidate, candidates_from_jobs, rank_portfolio_candidates
from control.services.workload_policy import evaluate_text
from markets.base import MarketAdapter, NormalizedOpportunity


ZERO = Decimal("0")
CENT = Decimal("0.01")
POLICY_CHECKED_AT = datetime(2026, 8, 16, 0, 0, tzinfo=datetime_timezone.utc)


class SourceClassification(StrEnum):
    BUILT_IN_DEMAND = "BUILT_IN_DEMAND"
    MARKETPLACE_DISCOVERY = "MARKETPLACE_DISCOVERY"
    DIRECT_DEMAND = "DIRECT_DEMAND"
    MARKETING_DEPENDENT = "MARKETING_DEPENDENT"


DAY_ONE_SOURCE_CLASSES = frozenset({SourceClassification.BUILT_IN_DEMAND, SourceClassification.MARKETPLACE_DISCOVERY})


@dataclass(frozen=True)
class EarnAction:
    key: str
    label: str
    markets: tuple[str, ...]
    discovery: str
    execution_workflow: tuple[str, ...]
    submission_workflow: tuple[str, ...]
    capability_requirements: tuple[str, ...]
    source_classification: SourceClassification = SourceClassification.MARKETPLACE_DISCOVERY
    assignment_required: bool = False


EARN_ACTIONS: tuple[EarnAction, ...] = (
    EarnAction("TASKBOUNTY_BUG_FIX", "TaskBounty bug fixes", ("taskbounty",), "REST/MCP funded task feed", ("repo_access", "isolated_clone", "inspect", "implement", "regression_test", "full_tests", "qa"), ("github_pr", "taskbounty_submission", "status", "bank_settlement"), ("coding", "git", "tests", "sandbox")),
    EarnAction("TASKBOUNTY_COVERAGE", "TaskBounty coverage uplift", ("taskbounty",), "REST/MCP funded coverage feed", ("repo_access", "isolated_clone", "baseline_coverage", "substantive_tests", "prove_uplift", "full_tests", "qa"), ("github_pr", "taskbounty_submission", "status", "bank_settlement"), ("coding", "coverage", "tests", "sandbox")),
    EarnAction("OPIRE_REWARD", "Opire rewarded GitHub issue", ("opire",), "bounded first-party/public reward listing", ("github_try", "isolated_clone", "implement", "tests", "qa"), ("github_pr", "github_claim", "creator_acceptance", "stripe_settlement"), ("coding", "git", "tests", "github_comments")),
    EarnAction("ALGORA_BOUNTY", "Algora GitHub bounty", ("algora",), "official API/SDK bounty resource", ("claim", "isolated_clone", "implement", "tests", "qa"), ("github_pr", "algora_claim", "status", "fiat_settlement"), ("coding", "git", "tests", "sandbox")),
    EarnAction("GITPAY_TASK", "Gitpay funded issue", ("gitpay",), "first-party task source", ("apply", "wait_for_assignment", "isolated_clone", "implement", "tests", "qa"), ("github_pr", "acceptance", "payment_request", "bank_or_paypal_settlement"), ("coding", "git", "tests", "explicit_assignment"), assignment_required=True),
    EarnAction("FUNDED_FEATURE_WORK", "Funded feature implementation", ("taskbounty", "opire", "algora", "gitpay"), "supported funded-work feeds", ("objective_scope", "isolated_clone", "implement", "tests", "qa"), ("market_delivery", "acceptance", "settlement"), ("coding", "git", "tests", "sandbox")),
    EarnAction("FUNDED_TEST_DOCS_REFACTOR", "Funded tests, docs, and refactor", ("taskbounty", "opire", "algora", "gitpay"), "supported funded-work feeds", ("isolated_clone", "bounded_change", "behavioral_proof", "full_tests", "qa"), ("market_delivery", "acceptance", "settlement"), ("coding", "git", "tests", "sandbox")),
    EarnAction("MARKETPLACE_API_ACTOR_INCOME", "Marketplace API and Actor demand", ("rapidapi", "apify-store"), "authenticated paid inbound request/event", ("quota_and_budget", "margin_admission", "canonical_operation", "artifact_or_result", "qa", "usage_record"), ("usage_reconciliation", "provider_payout", "settlement"), ("commercial_api", "usage_metering", "qa"), SourceClassification.BUILT_IN_DEMAND),
)
ACTION_BY_KEY = {action.key: action for action in EARN_ACTIONS}


def canonical_action(market: str, opportunity: NormalizedOpportunity) -> str:
    text = " ".join((opportunity.title, opportunity.task_class, str(opportunity.raw.get("type") or ""), str(opportunity.raw.get("category") or ""))).casefold()
    if market == "taskbounty" and "coverage" in text:
        return "TASKBOUNTY_COVERAGE"
    if any(term in text for term in ("documentation", " docs", "test", "refactor", "typing", "type hints", "ci improvement", "code cleanup")):
        return "FUNDED_TEST_DOCS_REFACTOR"
    if any(term in text for term in ("feature", "migration", "integration", "cli", "ui ", "api endpoint", "developer tooling")):
        return "FUNDED_FEATURE_WORK"
    if market == "taskbounty":
        return "TASKBOUNTY_BUG_FIX"
    return opportunity.action


@dataclass(frozen=True)
class MarketPolicy:
    market: str
    policy_source: tuple[str, ...]
    checked_at: datetime
    automation_allowed: bool
    machine_access_method: str
    terms_verified: bool
    payout_methods: tuple[str, ...]
    supported_payout_selected: str
    fees: dict[str, str]
    settlement_model: str
    work_types: tuple[str, ...]
    prohibited_work: tuple[str, ...]
    owner_actions: tuple[str, ...]
    credential_fields: tuple[str, ...]


_COMMON_PROHIBITED = (
    "external scanning/pentest/stress", "spam or engagement manipulation", "credential/CAPTCHA attacks",
    "continuous crawling", "copyright infringement", "local neural execution", "crypto or token settlement",
)


MARKET_POLICIES: dict[str, MarketPolicy] = {
    "taskbounty": MarketPolicy("taskbounty", ("https://www.task-bounty.com/docs/mcp", "https://www.task-bounty.com/for-agents/build-your-own", "https://www.task-bounty.com/terms"), POLICY_CHECKED_AT, True, "OFFICIAL_REST_AND_MCP", True, ("USD_BANK_TRANSFER",), "USD_BANK_TRANSFER", {"contributor_share": "0.80", "market_fee_rate": "0.20"}, "verified submission then provider bank payout; receipt required", ("bug", "coverage", "feature", "tests", "docs", "refactor"), _COMMON_PROHIBITED, ("create solver key", "complete bank payout onboarding", "prove receipt"), ("api_key",)),
    "opire": MarketPolicy("opire", ("https://docs.opire.dev/overview/getting-started", "https://docs.opire.dev/overview/commands"), POLICY_CHECKED_AT, False, "DOCUMENTED_GITHUB_COMMANDS", True, ("STRIPE",), "STRIPE", {"market_fee_rate": "UNVERIFIED"}, "creator confirmation/payment then Stripe receipt", ("rewarded_github_issue",), _COMMON_PROHIBITED, ("connect GitHub", "complete Stripe payout", "approve GitHub command mutation"), ()),
    "algora": MarketPolicy("algora", ("https://api.docs.algora.io/",), POLICY_CHECKED_AT, False, "OFFICIAL_API_SDK_READ_CLAIM_CONTRACT", True, ("PROVIDER_FIAT",), "PROVIDER_FIAT", {"market_fee_rate": "UNVERIFIED"}, "accepted claim then separately reconciled payout", ("github_bounty",), _COMMON_PROHIBITED, ("complete account/GitHub onboarding", "prove fiat payout", "configure official claim auth"), ()),
    "gitpay": MarketPolicy("gitpay", ("https://gitpay.me/", "https://github.com/worknenjoy/gitpay"), POLICY_CHECKED_AT, False, "FIRST_PARTY_SOURCE_OWNER_ASSISTED_MUTATIONS", True, ("BANK", "PAYPAL"), "BANK_OR_PAYPAL", {"market_fee_rate": "UNVERIFIED"}, "assignment, accepted PR, payment request, receipt", ("funded_git_issue",), _COMMON_PROHIBITED, ("open account", "apply", "record assignment", "request payment", "prove receipt"), ()),
    "rapidapi": MarketPolicy("rapidapi", ("https://docs.rapidapi.com/",), POLICY_CHECKED_AT, True, "AUTHENTICATED_INBOUND_PROXY_REQUEST", True, ("PAYPAL",), "PAYPAL", {"market_fee_rate": "0.25"}, "usage reporting then provider payout receipt", ("api_request", "subscription_usage", "overage"), _COMMON_PROHIBITED, ("open provider account", "publish API", "configure payout", "prove receipt"), ("proxy_secret",)),
    "apify-store": MarketPolicy("apify-store", ("https://docs.apify.com/actors/publishing/monetize/pay-per-event", "https://docs.apify.com/legal/store-publishing-terms-and-conditions"), POLICY_CHECKED_AT, True, "AUTHENTICATED_APIFY_EVENT_AND_RUN_API", True, ("PAYPAL", "WISE", "PROVIDER_FIAT"), "PROVIDER_FIAT", {"market_fee_rate": "0.20"}, "usage/event reporting then creator payout receipt", ("pay_per_event", "paid_result", "actor_run", "rental"), _COMMON_PROHIBITED, ("complete creator KYC", "publish Actor", "configure payout", "prove receipt"), ("api_token",)),
}


@dataclass(frozen=True)
class EarnControls:
    max_active_claims: int = 2
    max_new_claims_per_hour: int = 1
    max_market_concentration: Decimal = Decimal("0.60")
    min_expected_net_profit: Decimal = Decimal("5.00")
    min_expected_profit_per_minute: Decimal = Decimal("0.10")
    min_feasibility_score: Decimal = Decimal("0.65")
    max_provider_cost_at_risk: Decimal = Decimal("25.00")
    max_unsettled_market_exposure: Decimal = Decimal("250.00")
    max_repair_attempts: int = 1

    @classmethod
    def from_environment(cls) -> "EarnControls":
        return cls(
            max_active_claims=max(0, int(os.getenv("MAX_ACTIVE_CLAIMS", "2"))),
            max_new_claims_per_hour=max(0, int(os.getenv("MAX_NEW_CLAIMS_PER_HOUR", "1"))),
            max_market_concentration=Decimal(os.getenv("MAX_MARKET_CONCENTRATION", "0.60")),
            min_expected_net_profit=Decimal(os.getenv("MIN_EXPECTED_NET_PROFIT", "5.00")),
            min_expected_profit_per_minute=Decimal(os.getenv("MIN_EXPECTED_PROFIT_PER_MINUTE", "0.10")),
            min_feasibility_score=Decimal(os.getenv("MIN_FEASIBILITY_SCORE", "0.65")),
            max_provider_cost_at_risk=Decimal(os.getenv("MAX_PROVIDER_COST_AT_RISK", "25.00")),
            max_unsettled_market_exposure=Decimal(os.getenv("MAX_UNSETTLED_MARKET_EXPOSURE", "250.00")),
            max_repair_attempts=max(0, int(os.getenv("MAX_REPAIR_ATTEMPTS", "1"))),
        )


@dataclass(frozen=True)
class EconomicEstimate:
    advertised_reward: Decimal
    expected_settled_reward: Decimal
    marketplace_fee: Decimal
    provider_cost: Decimal
    execution_cost: Decimal
    expected_repair_cost: Decimal
    expected_rejection_cost: Decimal
    opportunity_cost: Decimal
    settlement_risk_reserve: Decimal
    expected_net: Decimal
    expected_minutes: Decimal
    expected_profit_per_minute: Decimal
    competition_factor: Decimal


@dataclass(frozen=True)
class AdmissionResult:
    allowed: bool
    reason_codes: tuple[str, ...]
    economics: EconomicEstimate
    feasibility_score: Decimal
    provider_spend_allowed: bool


def _probability(value: Any, default: str = "0") -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:
        parsed = Decimal(default)
    return max(ZERO, min(Decimal("1"), parsed))


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def competition_factor(competition: dict[str, Any]) -> Decimal:
    solvers = max(0, int(competition.get("solvers") or 0))
    attempts = max(0, int(competition.get("existing_attempts") or 0))
    pull_requests = max(0, int(competition.get("existing_prs") or 0))
    exclusive_bonus = Decimal("0.10") if competition.get("claim_exclusive") else ZERO
    crowd_penalty = min(Decimal("0.75"), Decimal(solvers) * Decimal("0.04") + Decimal(attempts) * Decimal("0.05") + Decimal(pull_requests) * Decimal("0.10"))
    return max(Decimal("0.10"), min(Decimal("1"), Decimal("1") - crowd_penalty + exclusive_bonus))


def estimate_expected_settled_profit(
    opportunity: NormalizedOpportunity,
    *,
    implementation_probability: Decimal = Decimal("0.80"),
    repair_cost: Decimal = ZERO,
    rejection_cost: Decimal = ZERO,
    opportunity_cost: Decimal = ZERO,
    settlement_risk_reserve: Decimal = ZERO,
) -> EconomicEstimate:
    reward = max(ZERO, Decimal(opportunity.reward))
    implementation = _probability(implementation_probability, "0.80")
    acceptance = _probability(opportunity.acceptance_probability, "0.50")
    payout = _probability(opportunity.payout_probability, "0.50")
    competition = competition_factor(opportunity.competition)
    expected_reward = reward * implementation * acceptance * payout * competition
    marketplace_fee = reward * max(ZERO, Decimal(opportunity.fee_rate)) * payout
    provider_cost = max(ZERO, Decimal(opportunity.expected_provider_cost))
    execution_cost = max(ZERO, Decimal(opportunity.expected_execution_cost))
    expected_net = expected_reward - marketplace_fee - provider_cost - execution_cost - repair_cost - rejection_cost - opportunity_cost - settlement_risk_reserve
    minutes = max(Decimal("1"), Decimal(opportunity.expected_minutes))
    return EconomicEstimate(
        advertised_reward=_money(reward),
        expected_settled_reward=_money(expected_reward),
        marketplace_fee=_money(marketplace_fee),
        provider_cost=_money(provider_cost),
        execution_cost=_money(execution_cost),
        expected_repair_cost=_money(repair_cost),
        expected_rejection_cost=_money(rejection_cost),
        opportunity_cost=_money(opportunity_cost),
        settlement_risk_reserve=_money(settlement_risk_reserve),
        expected_net=_money(expected_net),
        expected_minutes=minutes,
        expected_profit_per_minute=(expected_net / minutes).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        competition_factor=competition,
    )


def policy_is_current(policy: MarketPolicy, *, now: datetime | None = None, max_age_days: int | None = None) -> bool:
    now = now or timezone.now()
    max_age = max_age_days if max_age_days is not None else max(1, int(os.getenv("MARKET_POLICY_MAX_AGE_DAYS", "30")))
    return policy.terms_verified and policy.checked_at >= now - timedelta(days=max_age)


def admit_opportunity(
    opportunity: NormalizedOpportunity,
    *,
    market: str,
    feasibility_score: Decimal,
    controls: EarnControls | None = None,
    active_claims: int = 0,
    new_claims_last_hour: int = 0,
    market_concentration: Decimal = ZERO,
    unsettled_market_exposure: Decimal = ZERO,
) -> AdmissionResult:
    controls = controls or EarnControls.from_environment()
    economics = estimate_expected_settled_profit(opportunity, implementation_probability=feasibility_score)
    reasons: list[str] = []
    action = ACTION_BY_KEY.get(opportunity.action)
    policy = MARKET_POLICIES.get(market)
    try:
        source_class = SourceClassification(opportunity.source_classification)
    except ValueError:
        source_class = None
        reasons.append("SOURCE_CLASSIFICATION_UNKNOWN")
    body = "\n".join((opportunity.title, opportunity.task_class, str(opportunity.raw.get("description") or ""), str(opportunity.raw.get("requirements") or "")))
    workload = evaluate_text(body)
    reasons.extend(workload.reason_codes)
    if action is None:
        reasons.append("UNKNOWN_EARN_ACTION")
    if source_class not in DAY_ONE_SOURCE_CLASSES:
        reasons.append("MARKETING_DEPENDENT_EXCLUDED")
    if policy is None:
        reasons.append("MARKET_POLICY_MISSING")
    elif not policy_is_current(policy):
        reasons.append("MARKET_POLICY_STALE_OR_UNVERIFIED")
    elif not policy.automation_allowed and market not in {"rapidapi", "apify-store"}:
        reasons.append("READY_FOR_OWNER_ACTION")
    if action and action.assignment_required and opportunity.raw.get("assignment_confirmed") is not True:
        reasons.append("WAITING_FOR_EXPLICIT_ASSIGNMENT")
    if feasibility_score < controls.min_feasibility_score:
        reasons.append("FEASIBILITY_BELOW_FLOOR")
    if economics.expected_net < controls.min_expected_net_profit:
        reasons.append("EXPECTED_NET_BELOW_FLOOR")
    if economics.expected_profit_per_minute < controls.min_expected_profit_per_minute:
        reasons.append("PROFIT_PER_MINUTE_BELOW_FLOOR")
    if economics.provider_cost > controls.max_provider_cost_at_risk:
        reasons.append("PROVIDER_COST_AT_RISK_EXCEEDED")
    if active_claims >= controls.max_active_claims:
        reasons.append("MAX_ACTIVE_CLAIMS_REACHED")
    if new_claims_last_hour >= controls.max_new_claims_per_hour:
        reasons.append("MAX_NEW_CLAIMS_PER_HOUR_REACHED")
    if market_concentration > controls.max_market_concentration:
        reasons.append("MAX_MARKET_CONCENTRATION_EXCEEDED")
    if unsettled_market_exposure + opportunity.reward > controls.max_unsettled_market_exposure:
        reasons.append("MAX_UNSETTLED_MARKET_EXPOSURE_EXCEEDED")
    provider_spend_allowed = not reasons and economics.provider_cost <= controls.max_provider_cost_at_risk
    return AdmissionResult(not reasons, tuple(dict.fromkeys(reasons)), economics, feasibility_score, provider_spend_allowed)


def portfolio_candidate(opportunity: NormalizedOpportunity, admission: AdmissionResult, *, job_id: str, action_allowed: bool = False) -> PortfolioCandidate:
    payout = _probability(opportunity.payout_probability)
    acceptance = _probability(opportunity.acceptance_probability)
    concentration = _probability(opportunity.raw.get("market_concentration", 0))
    return PortfolioCandidate(
        job_id=job_id,
        source_type=opportunity.source_classification,
        revenue_channel="PAY_PER_CALL_API" if opportunity.action == "MARKETPLACE_API_ACTOR_INCOME" else "BOUNTY",
        expected_net_profit=admission.economics.expected_net,
        risk_adjusted_profit=admission.economics.expected_net,
        productive_minutes=admission.economics.expected_minutes,
        payout_probability=payout,
        acceptance_probability=acceptance,
        market_concentration=concentration,
        payment_risk=Decimal("1") - payout,
        eligible=admission.allowed,
        action_allowed=action_allowed and current_mode() in {AutonomyMode.LOW_RISK, AutonomyMode.FULL},
        selection_blockers=admission.reason_codes,
    )


def rank_autonomous_income(candidates: Iterable[tuple[NormalizedOpportunity, AdmissionResult, str]], *, available_slots: int, productive_minutes_available: Decimal) -> list[RankedCandidate]:
    rows = [portfolio_candidate(opportunity, admission, job_id=job_id, action_allowed=True) for opportunity, admission, job_id in candidates]
    return rank_portfolio_candidates(rows, available_slots=available_slots, productive_minutes_available=productive_minutes_available)


@transaction.atomic
def claim_once(*, adapter: MarketAdapter, job_id, admission: AdmissionResult) -> dict[str, Any]:
    job = Job.objects.select_for_update().select_related("marketplace").get(pk=job_id)
    existing = Claim.objects.filter(job=job).order_by("created_at").first()
    if existing:
        return {"performed": False, "idempotent": True, "claim_id": existing.id, "status": existing.status}
    if current_mode() not in {AutonomyMode.LOW_RISK, AutonomyMode.FULL}:
        return {"performed": False, "idempotent": True, "status": "AUTONOMOUS_MODE_OFF"}
    if not admission.allowed:
        return {"performed": False, "idempotent": True, "status": "ADMISSION_BLOCKED", "reasons": list(admission.reason_codes)}
    if not adapter.capabilities.claim:
        return {"performed": False, "idempotent": True, "status": "READY_FOR_OWNER_ACTION"}
    opportunity = adapter.normalize_job(job.normalized_payload)
    try:
        response = adapter.claim(opportunity)
    except NotImplementedError as exc:
        return {
            "performed": False,
            "idempotent": True,
            "status": "READY_FOR_CREDENTIAL",
            "error": str(exc),
        }
    except Exception as exc:
        claim = Claim.objects.create(job=job, status="UNKNOWN_REMOTE_STATE", remote_reference="")
        return {"performed": True, "idempotent": False, "claim_id": claim.id, "status": claim.status, "error": exc.__class__.__name__}
    reference = str((response or {}).get("id") or (response or {}).get("claim_id") or "")
    claim = Claim.objects.create(job=job, status="CLAIMED", remote_reference=reference)
    if job.state == Job.State.EXPECTED:
        job.state = Job.State.CLAIMED
        job.save(update_fields=["state", "updated_at"])
    return {"performed": True, "idempotent": False, "claim_id": claim.id, "status": claim.status}


def provider_spend_reservable(job: Job, admission: AdmissionResult, *, controls: EarnControls | None = None) -> tuple[bool, tuple[str, ...]]:
    controls = controls or EarnControls.from_environment()
    reasons: list[str] = []
    if not admission.provider_spend_allowed:
        reasons.append("ECONOMIC_ADMISSION_BLOCKED")
    if job.normalized_payload.get("assignment_required") and job.normalized_payload.get("assignment_confirmed") is not True:
        reasons.append("WAITING_FOR_EXPLICIT_ASSIGNMENT")
    existing = GenXCall.objects.filter(job=job, status__in=["SUBMITTED", "RUNNING", "UNKNOWN_REMOTE_STATE"]).aggregate(total=Sum("max_allowed_credits"))["total"] or ZERO
    if existing > controls.max_provider_cost_at_risk:
        reasons.append("PROVIDER_COST_AT_RISK_EXCEEDED")
    return not reasons, tuple(reasons)


def no_external_mutation_cycle(*, adapters: Iterable[MarketAdapter] = ()) -> dict[str, Any]:
    """Scheduler hook. OFF mode performs no adapter call, including claims/submissions."""
    mode = current_mode()
    if mode == AutonomyMode.OFF:
        return {"mode": mode.value, "external_mutations": 0, "adapter_calls": 0, "status": "OFF"}
    return {"mode": mode.value, "external_mutations": 0, "adapter_calls": 0, "status": "SHADOW_ONLY"}


def _opportunity_from_job(job: Job, adapter: MarketAdapter | None) -> NormalizedOpportunity:
    payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    if adapter is not None:
        opportunity = adapter.normalize_job(payload)
    else:
        try:
            score = job.jobscore
        except Exception:
            score = None
        opportunity = NormalizedOpportunity(
            external_id=job.external_id,
            title=job.title,
            task_class=job.task_class,
            reward=job.reward,
            currency=job.currency,
            raw=payload,
            action=str(payload.get("autonomous_action") or payload.get("action") or "FUNDED_FEATURE_WORK"),
            fee_rate=Decimal(str(payload.get("marketplace_fee_rate") or 0)),
            payout_probability=Decimal(str(payload.get("payout_probability") or getattr(score, "p_payment", "0.5"))),
            acceptance_probability=Decimal(str(payload.get("acceptance_probability") or getattr(score, "p_accept", "0.5"))),
            expected_provider_cost=Decimal(str(payload.get("expected_provider_cost") or getattr(score, "expected_genx_cost", 0))),
            expected_execution_cost=Decimal(str(payload.get("expected_execution_cost") or getattr(score, "expected_external_cost", 0))),
            expected_minutes=int(payload.get("expected_minutes") or getattr(score, "expected_minutes", 60)),
            source_classification=str(payload.get("source_classification") or SourceClassification.MARKETPLACE_DISCOVERY),
            competition=dict(payload.get("competition") or {}),
            capabilities_required=tuple(payload.get("capability_requirements") or ()),
        )
    action = str(payload.get("autonomous_action") or canonical_action(job.marketplace.slug, opportunity))
    return replace(
        opportunity,
        action=action,
        source_classification=str(payload.get("source_classification") or opportunity.source_classification),
        raw={**opportunity.raw, **payload},
    )


@transaction.atomic
def _persist_autonomous_ranking(jobs: list[Job], ranked: list[RankedCandidate]) -> None:
    by_id = {str(job.id): job for job in jobs}
    for row in ranked:
        job = by_id[row.candidate.job_id]
        PortfolioDecision.objects.create(
            job=job,
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
                "objective": "EXPECTED_SETTLED_PROFIT_PER_MINUTE",
                "eligible": row.candidate.eligible,
                "action_allowed": row.candidate.action_allowed,
                "selection_blockers": list(row.candidate.selection_blockers),
                "productive_minutes": str(row.candidate.productive_minutes),
                "would_select_if_enabled": row.would_select_if_enabled,
            },
        )


def run_autonomous_earn_loop(*, adapters: dict[str, MarketAdapter], available_slots: int | None = None, productive_minutes: Decimal = Decimal("480")) -> dict[str, Any]:
    """Rank canonical Jobs, claim idempotently, then dispatch existing WorkPlans.

    The OFF boundary is evaluated before any adapter call. Market/policy/payout,
    per-market switch, acquisition-preflight, and capacity gates remain owned by
    the existing portfolio and planning services.
    """
    mode = current_mode()
    if mode == AutonomyMode.OFF:
        return {"mode": mode.value, "ranked": 0, "claimed": 0, "dispatched": 0, "external_mutations": 0, "status": "OFF"}
    slots = max(0, available_slots if available_slots is not None else EarnControls.from_environment().max_active_claims)
    jobs = list(Job.objects.select_related("marketplace", "jobscore").filter(state=Job.State.EXPECTED, jobscore__isnull=False, normalized_payload__autonomous_action__in=tuple(ACTION_BY_KEY)).order_by("created_at")[:250])
    controls = EarnControls.from_environment()
    active_claim_rows = Claim.objects.exclude(job__state__in=[Job.State.SETTLED, Job.State.FAILED])
    active_claims = active_claim_rows.count()
    new_claims_last_hour = Claim.objects.filter(created_at__gte=timezone.now() - timedelta(hours=1)).count()
    active_by_market = {
        slug: active_claim_rows.filter(job__marketplace__slug=slug).count()
        for slug in {job.marketplace.slug for job in jobs}
    }
    unsettled_by_market = {
        slug: Job.objects.filter(marketplace__slug=slug, claims__isnull=False).exclude(state__in=[Job.State.SETTLED, Job.State.FAILED]).aggregate(total=Sum("reward"))["total"] or ZERO
        for slug in {job.marketplace.slug for job in jobs}
    }
    existing_truth = {row.job_id: row for row in candidates_from_jobs(jobs)}
    effective_admissions: dict[str, AdmissionResult] = {}
    opportunities: dict[str, NormalizedOpportunity] = {}
    candidates: list[PortfolioCandidate] = []
    for job in jobs:
        adapter = adapters.get(job.marketplace.slug)
        opportunity = _opportunity_from_job(job, adapter)
        feasibility = Decimal(str(job.jobscore.p_acquire))
        admission = admit_opportunity(
            opportunity,
            market=job.marketplace.slug,
            feasibility_score=feasibility,
            controls=controls,
            active_claims=active_claims,
            new_claims_last_hour=new_claims_last_hour,
            market_concentration=(Decimal(active_by_market.get(job.marketplace.slug, 0)) / Decimal(max(1, active_claims))),
            unsettled_market_exposure=Decimal(unsettled_by_market.get(job.marketplace.slug, ZERO)),
        )
        existing = existing_truth.get(str(job.id))
        existing_blockers = existing.selection_blockers if existing else ("NO_CANONICAL_PORTFOLIO_TRUTH",)
        reasons = tuple(dict.fromkeys((*admission.reason_codes, *existing_blockers)))
        effective = replace(
            admission,
            allowed=admission.allowed and bool(existing and existing.eligible and existing.action_allowed),
            reason_codes=reasons,
            provider_spend_allowed=admission.provider_spend_allowed and bool(existing and existing.eligible and existing.action_allowed),
        )
        job_id = str(job.id)
        opportunities[job_id] = opportunity
        effective_admissions[job_id] = effective
        candidates.append(portfolio_candidate(opportunity, effective, job_id=job_id, action_allowed=bool(existing and existing.action_allowed)))
    ranked = rank_portfolio_candidates(candidates, available_slots=slots, productive_minutes_available=max(ZERO, productive_minutes))
    _persist_autonomous_ranking(jobs, ranked)
    claims = []
    for row in ranked:
        if not row.selected:
            continue
        job = next(item for item in jobs if str(item.id) == row.candidate.job_id)
        adapter = adapters.get(job.marketplace.slug)
        if adapter is None:
            claims.append({"job": str(job.id), "status": "ADAPTER_NOT_CONFIGURED", "performed": False})
            continue
        admission = effective_admissions[str(job.id)]
        spend_allowed, spend_reasons = provider_spend_reservable(job, admission, controls=controls)
        if not spend_allowed:
            claims.append({"job": str(job.id), "status": "PROVIDER_SPEND_BLOCKED", "performed": False, "reasons": list(spend_reasons)})
            continue
        claims.append({"job": str(job.id), **claim_once(adapter=adapter, job_id=job.id, admission=admission)})
    from planning.services import dispatch_awarded_jobs

    dispatch = dispatch_awarded_jobs(limit=max(1, slots)) if any(item.get("performed") for item in claims) else {"queued": 0}
    return {
        "mode": mode.value,
        "ranked": len(ranked),
        "claimed": sum(bool(item.get("performed")) for item in claims),
        "dispatched": int(dispatch.get("queued") or 0),
        "external_mutations": sum(bool(item.get("performed")) for item in claims),
        "claims": claims,
        "status": "ACTIVE",
    }


def run_autonomous_daily_cycle(*, adapters: dict[str, MarketAdapter], limit: int = 100) -> dict[str, Any]:
    """Perform bounded read/reconciliation and canonical performance refresh."""
    mode = current_mode()
    if mode == AutonomyMode.OFF:
        return {"mode": mode.value, "reconciled": 0, "stale_claims": 0, "performance_rows": 0, "external_mutations": 0, "status": "OFF"}
    reconciled = 0
    stale_claims = 0
    cutoff = timezone.now() - timedelta(hours=max(1, int(os.getenv("STALE_CLAIM_HOURS", "24"))))
    for claim in Claim.objects.select_related("job__marketplace").filter(job__state__in=[Job.State.CLAIMED, Job.State.AWARDED, Job.State.SUBMITTED, Job.State.ACCEPTED, Job.State.PAYOUT_PENDING]).order_by("updated_at")[: max(1, min(limit, 250))]:
        adapter = adapters.get(claim.job.marketplace.slug)
        if adapter is None or not adapter.capabilities.status:
            stale_claims += int(claim.updated_at < cutoff)
            continue
        try:
            adapter.reconcile(_opportunity_from_job(claim.job, adapter))
            reconciled += 1
        except (NotImplementedError, ValueError):
            stale_claims += int(claim.updated_at < cutoff)
    from control.services.profit_brain import refresh_performance

    performance = refresh_performance()
    return {
        "mode": mode.value,
        "reconciled": reconciled,
        "stale_claims": stale_claims,
        "performance_rows": len(performance),
        "external_mutations": 0,
        "status": "ACTIVE",
    }


def run_autonomous_frequent_cycle(*, adapters: dict[str, MarketAdapter], limit: int = 50) -> dict[str, Any]:
    mode = current_mode()
    if mode == AutonomyMode.OFF:
        return {"mode": mode.value, "discovered": 0, "reconciled": 0, "external_mutations": 0, "status": "OFF"}
    from control.services.markets import sync_market_discovery

    discovery = {}
    for slug, adapter in list(adapters.items())[:8]:
        try:
            discovery[slug] = sync_market_discovery(slug, adapter=adapter, limit=limit)
        except Exception as exc:
            discovery[slug] = {"discovered": 0, "blocked": exc.__class__.__name__}
    reconciled = 0
    for job in Job.objects.select_related("marketplace").filter(state__in=[Job.State.SUBMITTED, Job.State.ACCEPTED, Job.State.PAYOUT_PENDING]).order_by("updated_at")[: max(1, min(limit, 100))]:
        adapter = adapters.get(job.marketplace.slug)
        if adapter is None or not adapter.capabilities.status:
            continue
        try:
            adapter.reconcile(adapter.normalize_job(job.normalized_payload))
            reconciled += 1
        except (NotImplementedError, ValueError):
            continue
    return {"mode": mode.value, "discovery": discovery, "discovered": sum(int(row.get("discovered") or 0) for row in discovery.values()), "reconciled": reconciled, "external_mutations": 0, "status": "ACTIVE"}


def _dashboard_state(job: Job) -> str:
    latest_execution = job.executions.order_by("-attempt").first()
    latest_submission = Submission.objects.filter(job=job).order_by("-version").first()
    payout = Payout.objects.filter(job=job).first()
    if payout and payout.state == Payout.State.SETTLED:
        return "SETTLED"
    if payout and payout.state == Payout.State.PAYOUT_PENDING:
        return "PAYOUT_PENDING"
    if job.state == Job.State.FAILED:
        return "REJECTED"
    if latest_submission and latest_submission.status == "SUBMITTED":
        return "AWAITING_REVIEW"
    if latest_execution and latest_execution.status == "QA_PASSED":
        return "TESTING"
    return {
        Job.State.DISCOVERED: "AVAILABLE_NOW", Job.State.EXPECTED: "AVAILABLE_NOW",
        Job.State.CLAIMED: "CLAIMED", Job.State.AWARDED: "CLAIMED",
        Job.State.EXECUTING: "WORKING", Job.State.SUBMITTED: "SUBMITTED",
        Job.State.ACCEPTED: "ACCEPTED", Job.State.PAYOUT_PENDING: "PAYOUT_PENDING",
        Job.State.SETTLED: "SETTLED", Job.State.FAILED: "REJECTED",
    }.get(job.state, "SKIPPED")


def autonomous_earn_snapshot() -> dict[str, Any]:
    from control.services.profit_brain import settled_profit_truth

    now = timezone.now()
    jobs = list(Job.objects.select_related("marketplace").prefetch_related("portfolio_decisions", "executions").order_by("-created_at")[:250])
    rows = []
    for job in jobs:
        payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
        action = str(payload.get("autonomous_action") or payload.get("action") or "")
        channel = str(payload.get("revenue_channel") or "")
        if action not in ACTION_BY_KEY and channel not in {"PAY_PER_CALL_API", "BOUNTY"}:
            continue
        score = getattr(job, "jobscore", None)
        decision = job.portfolio_decisions.order_by("-created_at").first()
        payout = Payout.objects.filter(job=job).first()
        submission = Submission.objects.filter(job=job).order_by("-version").first()
        rows.append({
            "job": str(job.id), "source": job.marketplace.slug, "type": action or job.task_class,
            "reward": str(job.reward), "fee": str(payload.get("marketplace_fee") or "0"),
            "expected_provider_cost": str(getattr(score, "expected_genx_cost", ZERO)),
            "expected_net": str(getattr(score, "expected_profit", ZERO)),
            "expected_profit_per_minute": str(getattr(score, "expected_profit_per_minute", ZERO)),
            "acceptance_probability": str(getattr(score, "p_accept", ZERO)),
            "payout_probability": str(getattr(score, "p_payment", ZERO)),
            "competition": payload.get("competition") or {}, "deadline": job.deadline.isoformat() if job.deadline else None,
            "capabilities": payload.get("capability_requirements") or [],
            "reason_selected": "EXPECTED_SETTLED_PROFIT_RANK" if decision and decision.selected else "",
            "reason_rejected": list((decision.inputs or {}).get("selection_blockers") or []) if decision else [],
            "current_state": _dashboard_state(job),
            "submission": submission.status if submission else "NOT_SUBMITTED",
            "settlement": payout.state if payout else "NOT_EARNED",
        })
    def settled_since(start):
        return Payout.objects.filter(state=Payout.State.SETTLED, settled_at__gte=start).aggregate(total=Sum("net"))["total"] or ZERO
    active_states = {"CLAIMED", "WORKING", "TESTING", "SUBMITTED", "AWAITING_REVIEW", "ACCEPTED", "PAYOUT_PENDING"}
    profit_truth = settled_profit_truth(start=now - timedelta(days=30))
    return {
        "section": "autonomous-earn",
        "cards": [
            {"label": "OPEN PAID DEMAND", "value": sum(row["current_state"] == "AVAILABLE_NOW" for row in rows), "truth": "Persisted compatible demand only"},
            {"label": "QUALIFIED DEMAND", "value": sum(bool(row["reason_selected"]) for row in rows), "truth": "Economically ranked"},
            {"label": "CAPITAL/PROVIDER COST AT RISK", "value": str(sum((Decimal(row["expected_provider_cost"]) for row in rows if row["current_state"] in active_states), ZERO)), "truth": "Expected provider cost for active work"},
            {"label": "EXPECTED NET FROM ACTIVE WORK", "value": str(sum((Decimal(row["expected_net"]) for row in rows if row["current_state"] in active_states), ZERO)), "truth": "Forecast, not revenue"},
            {"label": "SUBMITTED VALUE", "value": str(sum((Decimal(row["reward"]) for row in rows if row["current_state"] in {"SUBMITTED", "AWAITING_REVIEW"}), ZERO)), "truth": "Not settled"},
            {"label": "ACCEPTED VALUE", "value": str(sum((Decimal(row["reward"]) for row in rows if row["current_state"] == "ACCEPTED"), ZERO)), "truth": "Not settled until receipt"},
            {"label": "PAYOUT PENDING", "value": str(sum((Decimal(row["reward"]) for row in rows if row["current_state"] == "PAYOUT_PENDING"), ZERO)), "truth": "Receivable, not cash"},
            {"label": "SETTLED TODAY", "value": str(settled_since(now.replace(hour=0, minute=0, second=0, microsecond=0))), "truth": "Authoritative settled payout only"},
            {"label": "SETTLED 7D", "value": str(settled_since(now - timedelta(days=7))), "truth": "Authoritative settled payout only"},
            {"label": "SETTLED 30D", "value": str(settled_since(now - timedelta(days=30))), "truth": "Authoritative settled payout only"},
            {"label": "TRUE NET PROFIT", "value": str(profit_truth.net_settled_profit), "truth": "Settled cash less attributable paid execution cost; 30d"},
        ],
        "rows": rows,
        "meta": {"autonomous_mode": current_mode().value, "actions": len(EARN_ACTIONS), "external_mutations_performed": False},
    }
