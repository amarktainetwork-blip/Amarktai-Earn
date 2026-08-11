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


# Commercial ordering is explicit and owner-specific. A payout may be automatic
# into PayPal/crypto/platform balance while the final bank withdrawal is human.
# Human withdrawal does not block autonomous earning. An unavailable owner rail
# (notably Stripe for this owner) is a hard practical priority penalty.
# Scores are planning inputs, never earnings promises or readiness claims.
PRIORITIES: dict[str, MarketPriority] = {
    "lemon-squeezy": MarketPriority(
        1, "ACTIVATE_FIRST", "OPEN_ACCOUNT_CONNECT_PAYPAL_AND_PUBLISH", 5, 5, 5, "HIGH",
        "Automatic scheduled payout to verified PayPal; human withdrawal happens outside AmarktAI",
        "South Africa is supported and recurring products/subscriptions can earn without winning each job manually.",
    ),
    "taskbounty": MarketPriority(
        2, "ACTIVATE_FIRST", "OPEN_SOLVER_ACCOUNT_AND_SET_CRYPTO_PAYOUT", 5, 5, 3, "HIGH",
        "Headless public crypto payout address; private keys and any cash-out remain outside Webdock",
        "API-native bounty discovery/submission plus headless crypto payout gives the cleanest autonomous work-to-payment path, although bounty supply is lumpy.",
    ),
    "rapidapi": MarketPriority(
        3, "ACTIVATE_FIRST", "OPEN_PROVIDER_ACCOUNT_LINK_PAYPAL_AND_PUBLISH", 5, 5, 5, "HIGH",
        "Automatic provider payout to PayPal; human South African withdrawal happens later outside AmarktAI",
        "Recurring API subscriptions and usage have a high autonomous ceiling; the 25% marketplace fee must be priced in.",
    ),
    "apify-store": MarketPriority(
        4, "ACTIVATE_FIRST", "OPEN_CREATOR_ACCOUNT_LINK_PAYOUT_AND_PUBLISH", 5, 4, 5, "HIGH",
        "Automatic monthly creator payout to PayPal/Wise; human withdrawal can occur later",
        "Paid Actors can earn repeatedly while scraper-heavy execution stays on Apify infrastructure rather than Webdock.",
    ),
    "contra": MarketPriority(
        5, "ACTIVATE_FIRST", "OPEN_ACCOUNT_AND_PROVE_PAYOUT", 3, 5, 5, "HIGH",
        "Contra wallet followed by PayPal, Payoneer or crypto withdrawal; final withdrawal is human",
        "Very high project/direct-sales upside and usable non-Stripe payout choices make it worth onboarding early even though acquisition is less API-native.",
    ),
    "dealwork": MarketPriority(
        6, "PROVE_PAYOUT", "COMPLETE_KYA_AND_WITHDRAWAL_PROOF", 4, 4, 3, "MEDIUM",
        "Automatic platform-wallet receipt after approval; human final withdrawal route still requires proof",
        "Good bounded live-job proving path; platform-wallet balance must not be confused with final settled cash.",
    ),
    "nevermined": MarketPriority(
        7, "PROVE_PAYOUT", "OPEN_ACCOUNT_AND_PROVE_CRYPTO_OR_PAYPAL_ROUTE", 5, 3, 5, "MEDIUM",
        "Automatic per-request settlement where supported; crypto custody/signing stays off Webdock",
        "Strong agent/API monetisation fit, but this owner's usable settlement route must be proven before activation.",
    ),
    "skyfire": MarketPriority(
        8, "PROVE_PAYOUT", "OPEN_SELLER_ACCOUNT_AND_PROVE_SETTLEMENT", 5, 3, 4, "MEDIUM",
        "Agent payment-token settlement after seller onboarding; final withdrawal can be human/off-host",
        "Strong machine-commerce design, but seller approval and a usable owner payout route remain unproven.",
    ),
    "algora": MarketPriority(
        9, "OPPORTUNISTIC", "OPEN_ACCOUNT_AND_PROVE_SOLVER_PAYOUT", 2, 4, 4, "MEDIUM",
        "Bounty reward after accepted claim/PR using the platform payout route",
        "Individual wins can be valuable; keep opportunistic until solver mutation and payout are proven.",
    ),
    "opire": MarketPriority(
        10, "OPPORTUNISTIC", "PAUSE_STRIPE_ONLY_OWNER_PAYOUT", 0, 0, 4, "HIGH",
        "Developer payout requires Stripe, which is unavailable to this owner",
        "Do not spend activation effort on a route that cannot currently pay this owner. Freelancer.com is the practical replacement work marketplace.",
    ),
    "coinbase-x402-bazaar": MarketPriority(
        11, "BUILD_OFFHOST", "BUILD_EXTERNAL_SETTLEMENT_BRIDGE_AFTER_CORE", 5, 3, 5, "MEDIUM",
        "Autonomous x402/on-chain settlement through an external wallet/CASP bridge",
        "High machine-to-machine ceiling, but signing, wallet and chain execution must stay off Webdock.",
    ),
    "swarms": MarketPriority(
        12, "BACKLOG", "VERIFY_PUBLISHING_AND_PAYOUT", 2, 2, 4, "LOW",
        "Unverified marketplace payout route",
        "Potential reusable-agent sales are attractive, but publishing contract and payout route need proof first.",
    ),
    "hyrve": MarketPriority(
        13, "BACKLOG", "VERIFY_API_AND_NON_STRIPE_PAYOUT", 1, 2, 3, "LOW",
        "Claimed Stripe/bank withdrawal route requiring account proof",
        "Potential work exists, but current payout and public automation are too uncertain for near-term effort.",
    ),
    "agentverserun": MarketPriority(
        14, "BACKLOG", "VERIFY_REAL_AUTOMATION_AND_NON_STRIPE_PAYOUT", 1, 2, 3, "LOW",
        "Claimed Stripe settlement requiring account proof",
        "Storefront/project potential remains secondary until a usable payout route and automation contract are demonstrated.",
    ),
    "virtuals-acp": MarketPriority(
        15, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 5, "LOW",
        "Off-host on-chain settlement only",
        "Potentially high autonomous agent-commerce ceiling, but it requires a separate compliant bridge and stronger demand proof.",
    ),
    "olas-mech": MarketPriority(
        16, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 4, "LOW",
        "Off-host on-chain settlement only",
        "Agent-call monetisation is conceptually strong but less immediate than proven PayPal/crypto channels.",
    ),
    "masumi-sokosumi": MarketPriority(
        17, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 4, "LOW",
        "Off-host on-chain settlement only",
        "Future agent-to-agent commerce lane after external wallet/CASP settlement is operational.",
    ),
    "singularitynet": MarketPriority(
        18, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 4, "LOW",
        "Off-host on-chain settlement only",
        "Potential service revenue, but it must not delay simpler usable channels.",
    ),
    "fetch-agentverse": MarketPriority(
        19, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 4, "LOW",
        "Off-host blockchain settlement only",
        "Keep the blockchain path external and revisit after the core treasury bridge is proven.",
    ),
    "okx-ai": MarketPriority(
        20, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 3, "LOW",
        "Off-host wallet settlement only",
        "Potential agent ecosystem income is currently less proven than higher-ranked routes.",
    ),
    "agrenting": MarketPriority(
        21, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 3, "LOW",
        "Off-host wallet/on-chain settlement only",
        "Do not spend core implementation time until demand and the external bridge are proven.",
    ),
    "agentgigs": MarketPriority(
        22, "BACKLOG", "PAUSE_UNTIL_NON_STRIPE_PAYOUT_EXISTS", 0, 0, 4, "HIGH",
        "Current owner payout contract requires Stripe Connect, which is unavailable to this owner",
        "The work API is attractive, but an unusable owner payout rail makes implementation pointless until the platform offers another route.",
    ),
    "callboard": MarketPriority(
        23, "BACKLOG", "PAUSE_UNTIL_NON_STRIPE_PAYOUT_EXISTS", 0, 0, 4, "HIGH",
        "Current owner payout contract requires Stripe Connect, which is unavailable to this owner",
        "Automation is strong but the owner cannot use the current payout rail, so this must not consume near-term implementation time.",
    ),
    "clawrr": MarketPriority(
        24, "BACKLOG", "RESEARCH_AFTER_CORE_CHANNELS", 4, 1, 2, "LOW",
        "Off-host settlement only",
        "Commercial and payout evidence is too weak for near-term activation.",
    ),
    "planetloga": MarketPriority(
        25, "BACKLOG", "RESEARCH_AFTER_CORE_CHANNELS", 4, 1, 2, "LOW",
        "Off-host settlement only",
        "Commercial and payout evidence is too weak for near-term activation.",
    ),
    "agentmarket": MarketPriority(
        26, "ARCHIVE", "REMOVE_FROM_ACTIVE_DASHBOARD", 0, 1, 0, "LOW",
        "Internal credits; redeemable cash not proven",
        "Do not present test/internal credits as an earning market until cash redemption is independently proven.",
    ),
    "chowdr": MarketPriority(
        27, "ARCHIVE", "REMOVE_FROM_ACTIVE_DASHBOARD", 0, 1, 0, "LOW",
        "Beta/test credits; real money not proven",
        "Do not present beta credits as an earning market until a real-money payout route exists.",
    ),
}


CANONICAL_EARNING_MARKETS = frozenset(set(LEGACY_MARKETS) | set(REVENUE_MARKETS))
ARCHIVED_MARKETS = frozenset(slug for slug, priority in PRIORITIES.items() if priority.tier == "ARCHIVE")

# Only routes that have a practical owner-usable payout path and enough contract
# evidence to justify near-term activation stay in the owner earning control plane.
# Everything else remains catalogued for audit/research without pretending it can
# earn this owner money today.
CURRENTLY_PAYABLE_MARKETS = frozenset({
    "lemon-squeezy",
    "taskbounty",
    "rapidapi",
    "apify-store",
    "contra",
    "dealwork",
    "nevermined",
    "algora",
})
ACTIVE_MARKETS = CURRENTLY_PAYABLE_MARKETS
INACTIVE_MARKETS = frozenset(CANONICAL_EARNING_MARKETS - ACTIVE_MARKETS - ARCHIVED_MARKETS)

if set(PRIORITIES) != CANONICAL_EARNING_MARKETS:
    missing = sorted(CANONICAL_EARNING_MARKETS - set(PRIORITIES))
    extra = sorted(set(PRIORITIES) - CANONICAL_EARNING_MARKETS)
    raise RuntimeError(f"market priority coverage mismatch missing={missing} extra={extra}")

if sorted(priority.rank for priority in PRIORITIES.values()) != list(range(1, 28)):
    raise RuntimeError("market priority ranks must be unique and contiguous 1..27")

if ACTIVE_MARKETS & ARCHIVED_MARKETS or ACTIVE_MARKETS & INACTIVE_MARKETS or ARCHIVED_MARKETS & INACTIVE_MARKETS:
    raise RuntimeError("market scope sets must be disjoint")
if ACTIVE_MARKETS | ARCHIVED_MARKETS | INACTIVE_MARKETS != CANONICAL_EARNING_MARKETS:
    raise RuntimeError("market scope must cover the canonical catalog")


def priority_for(market_slug: str) -> MarketPriority:
    return PRIORITIES[market_slug]
