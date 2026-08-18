from __future__ import annotations

from dataclasses import dataclass

from markets.catalog import BY_SLUG as LEGACY_MARKETS
from markets.revenue_catalog import BY_SLUG as REVENUE_MARKETS


@dataclass(frozen=True)
class MarketPriority:
    rank: int
    tier: str
    action: str
    payout_autonomy_score: int
    south_africa_setup_score: int
    autonomous_earning_ceiling_score: int
    confidence: str
    payout_path: str
    priority_reason: str


_ROWS = (
    ("taskbounty", "ACTIVATE_FIRST", "OPEN_SOLVER_ACCOUNT_AND_COMPLETE_BANK_PAYOUT", 4, 4, 4, "HIGH", "USD bank transfer after owner dashboard onboarding", "Funded machine-readable coding work; the adapter never configures optional token payout methods."),
    ("rapidapi", "ACTIVATE_FIRST", "OPEN_PROVIDER_ACCOUNT_LINK_PAYPAL_AND_PUBLISH", 5, 5, 5, "HIGH", "PayPal provider payout", "Existing paid API requests can recur without outbound marketing."),
    ("apify-store", "ACTIVATE_FIRST", "OPEN_CREATOR_ACCOUNT_AND_PROVE_FIAT_PAYOUT", 5, 4, 5, "HIGH", "PayPal, Wise, or approved provider payout", "Paid Actor demand executes on Apify while Webdock remains the control plane."),
    ("algora", "ACTIVATE_FIRST", "OPEN_ACCOUNT_AND_PROVE_FIAT_PAYOUT", 3, 4, 4, "MEDIUM", "Approved provider bank or PayPal payout", "Funded GitHub bounty demand is useful once claim and settlement proof are complete."),
    ("opire", "OWNER_ACTION", "CONNECT_GITHUB_AND_STRIPE_PAYOUT", 2, 2, 4, "HIGH", "Stripe payout after creator confirmation", "GitHub-native reward commands are documented, but creator payment risk and owner payout onboarding remain explicit."),
    ("gitpay", "OWNER_ACTION", "COMPLETE_ACCOUNT_ASSIGNMENT_AND_FIAT_PAYOUT", 2, 4, 3, "MEDIUM", "Bank or PayPal", "Funded tasks are useful only after explicit assignment; pre-assignment provider spend is prohibited."),
    ("lemon-squeezy", "ACTIVATE_NEXT", "OPEN_ACCOUNT_AND_PROVE_BANK_PAYOUT", 5, 5, 5, "HIGH", "Provider-managed fiat payout", "Recurring commerce is viable but marketing-dependent demand is excluded from day-one autonomous capacity."),
    ("contra", "ACTIVATE_NEXT", "OPEN_ACCOUNT_AND_PROVE_FIAT_PAYOUT", 2, 4, 3, "MEDIUM", "PayPal or Payoneer", "Manual acquisition keeps it outside the day-one autonomous queue."),
    ("dealwork", "PROVE_PAYOUT", "COMPLETE_KYA_AND_FIAT_WITHDRAWAL_PROOF", 3, 3, 3, "MEDIUM", "Provider balance followed by proven fiat withdrawal", "Balance is never treated as settled cash."),
    ("agentgigs", "BACKLOG", "PAUSE_UNTIL_OWNER_USABLE_FIAT_PAYOUT", 0, 0, 4, "HIGH", "Stripe Connect not owner-proven", "An unusable receipt rail blocks autonomous acquisition."),
    ("callboard", "BACKLOG", "PAUSE_UNTIL_OWNER_USABLE_FIAT_PAYOUT", 0, 0, 4, "HIGH", "Stripe Connect not owner-proven", "The work API cannot override missing payout proof."),
    ("nevermined", "BACKLOG", "VERIFY_FIAT_PLAN_AND_STRIPE_PAYOUT", 2, 1, 4, "MEDIUM", "Fiat Stripe Connect only", "Only fiat plans may be considered; all token-priced plans fail closed."),
    ("skyfire", "BACKLOG", "VERIFY_CARD_OR_BANK_SETTLEMENT", 2, 2, 3, "MEDIUM", "Card or bank settlement only", "Seller approval and an owner-usable normal payout route remain unproven."),
    ("hyrve", "BACKLOG", "VERIFY_FIRST_PARTY_CONTRACT_AND_FIAT_PAYOUT", 1, 2, 3, "LOW", "Bank withdrawal claimed, proof required", "No public mutation contract has been verified."),
    ("swarms", "BACKLOG", "VERIFY_PUBLISHING_AND_FIAT_PAYOUT", 1, 1, 3, "LOW", "Unverified normal payout route", "Publishing and cash settlement need first-party proof."),
    ("agentverserun", "BACKLOG", "VERIFY_MACHINE_ACCESS_AND_FIAT_PAYOUT", 1, 1, 3, "LOW", "Unverified fiat payout", "Manual storefront demand is not day-one autonomous income."),
    ("agentmarket", "ARCHIVE", "REMOVE_FROM_ACTIVE_DASHBOARD", 0, 1, 0, "LOW", "Internal credits are not cash", "Credits cannot enter revenue accounting without withdrawal proof."),
    ("chowdr", "ARCHIVE", "REMOVE_FROM_ACTIVE_DASHBOARD", 0, 1, 0, "LOW", "Test credits are not cash", "Beta credits cannot enter revenue accounting."),
)


PRIORITIES: dict[str, MarketPriority] = {
    slug: MarketPriority(index, tier, action, payout, south_africa, ceiling, confidence, path, reason)
    for index, (slug, tier, action, payout, south_africa, ceiling, confidence, path, reason) in enumerate(_ROWS, start=1)
}

CANONICAL_EARNING_MARKETS = frozenset(set(LEGACY_MARKETS) | set(REVENUE_MARKETS))
ARCHIVED_MARKETS = frozenset(slug for slug, priority in PRIORITIES.items() if priority.tier == "ARCHIVE")
CURRENTLY_PAYABLE_MARKETS = frozenset({
    "taskbounty", "rapidapi", "apify-store", "algora", "opire", "gitpay",
    "lemon-squeezy", "contra", "dealwork",
})
ACTIVE_MARKETS = CURRENTLY_PAYABLE_MARKETS
INACTIVE_MARKETS = frozenset(CANONICAL_EARNING_MARKETS - ACTIVE_MARKETS - ARCHIVED_MARKETS)

if set(PRIORITIES) != CANONICAL_EARNING_MARKETS:
    missing = sorted(CANONICAL_EARNING_MARKETS - set(PRIORITIES))
    extra = sorted(set(PRIORITIES) - CANONICAL_EARNING_MARKETS)
    raise RuntimeError(f"market priority coverage mismatch missing={missing} extra={extra}")
if sorted(priority.rank for priority in PRIORITIES.values()) != list(range(1, len(PRIORITIES) + 1)):
    raise RuntimeError("market priority ranks must be unique and contiguous")
if ACTIVE_MARKETS & ARCHIVED_MARKETS or ACTIVE_MARKETS & INACTIVE_MARKETS or ARCHIVED_MARKETS & INACTIVE_MARKETS:
    raise RuntimeError("market scope sets must be disjoint")
if ACTIVE_MARKETS | ARCHIVED_MARKETS | INACTIVE_MARKETS != CANONICAL_EARNING_MARKETS:
    raise RuntimeError("market scope must cover the canonical catalog")


def priority_for(market_slug: str) -> MarketPriority:
    return PRIORITIES[market_slug]
