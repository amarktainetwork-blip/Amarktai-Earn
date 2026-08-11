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


# Commercial ordering is intentionally explicit instead of pretending that every
# catalogued market has equal value. Ordering is decided by:
#   1. autonomous payout/settlement after one-time owner KYC/KYA,
#   2. South African owner setup friction,
#   3. scalable autonomous earning ceiling,
# with real-money/payout evidence acting as a fail-closed maturity gate.
#
# Scores are prioritisation inputs, not earnings promises. Actual readiness still
# comes exclusively from market_readiness() and observed settlement evidence.
PRIORITIES: dict[str, MarketPriority] = {
    "lemon-squeezy": MarketPriority(
        1, "ACTIVATE_FIRST", "CONNECT_AND_PUBLISH", 5, 5, 5, "HIGH",
        "Automatic scheduled South African bank payout or PayPal after merchant KYC",
        "South Africa is explicitly supported for bank payouts; direct products, subscriptions and usage-style sales can recur without winning each job manually.",
    ),
    "rapidapi": MarketPriority(
        2, "ACTIVATE_FIRST", "CONNECT_AND_PUBLISH", 5, 5, 5, "HIGH",
        "Automatic provider payout to PayPal; South African PayPal can withdraw through the local FNB-linked route",
        "Recurring API subscriptions and overages have a high autonomous ceiling, although the 25% marketplace fee and delayed payout cycle must be priced in.",
    ),
    "apify-store": MarketPriority(
        3, "ACTIVATE_FIRST", "CONNECT_AND_PUBLISH", 5, 4, 5, "HIGH",
        "Automatic monthly creator payout after KYC; PayPal/Wise and other payout methods are supported by Apify",
        "Paid Actors can earn repeatedly while scraper-heavy execution stays on Apify infrastructure rather than Webdock.",
    ),
    "taskbounty": MarketPriority(
        4, "ACTIVATE_FIRST", "CONNECT_PAYOUT_AND_PROVE", 5, 4, 3, "HIGH",
        "Headless public crypto payout address or bank payout; private wallet keys remain off Webdock",
        "The agent API supports autonomous bounty discovery/submission and headless crypto payout configuration; revenue is real but bounty inventory is naturally lumpy.",
    ),
    "nevermined": MarketPriority(
        5, "PROVE_PAYOUT", "PROVE_OFFHOST_OR_SA_SETTLEMENT", 5, 3, 5, "HIGH",
        "Automatic per-request fiat or stablecoin settlement; stablecoin custody/signing must remain off Webdock",
        "Excellent agent-to-agent and API monetisation fit with automatic metering, but the South African fiat route must be proven or replaced by an approved off-host settlement route.",
    ),
    "agentgigs": MarketPriority(
        6, "PROVE_PAYOUT", "PROVE_SA_STRIPE_CONNECT", 5, 2, 4, "HIGH",
        "Automatic Stripe Connect release after approved delivery and cooldown",
        "Job operations are API-native and autonomous after one-time human onboarding, but the owner's South African Connect payout capability remains the critical gate.",
    ),
    "callboard": MarketPriority(
        7, "PROVE_PAYOUT", "PROVE_SA_STRIPE_CONNECT", 5, 2, 4, "HIGH",
        "Stripe Connect payout after owner claim and payout onboarding",
        "The public API is strongly automation-oriented, but South African connected-account payout proof is still required before it can be treated as a cash route.",
    ),
    "contra": MarketPriority(
        8, "PROVE_PAYOUT", "ONBOARD_OWNER_AND_PROVE_PAYOUT", 3, 4, 5, "HIGH",
        "Contra wallet followed by local bank, PayPal, Payoneer or crypto withdrawal depending on account eligibility",
        "Very high project and direct-sales upside with several global payout methods, but public acquisition/payout automation is weaker than the API-native channels above.",
    ),
    "dealwork": MarketPriority(
        9, "PROVE_PAYOUT", "COMPLETE_KYA_AND_WITHDRAWAL_PROOF", 4, 3, 3, "MEDIUM",
        "Automatic platform-wallet receipt after approval; final withdrawal rail still requires proof",
        "Good bounded live-job proving path because escrow and agent work are native, but platform-wallet balance must not be confused with final settled cash.",
    ),
    "coinbase-x402-bazaar": MarketPriority(
        10, "BUILD_OFFHOST", "BUILD_EXTERNAL_SETTLEMENT_BRIDGE", 5, 3, 5, "MEDIUM",
        "Autonomous x402/on-chain settlement through an external wallet/CASP bridge",
        "High machine-to-machine monetisation ceiling, but all signing, wallet and chain execution must remain off Webdock and commercial demand must be proven.",
    ),
    "skyfire": MarketPriority(
        11, "PROVE_PAYOUT", "PROVE_SELLER_KYA_AND_SETTLEMENT", 5, 2, 4, "MEDIUM",
        "Agent payment-token settlement after seller onboarding",
        "Strong machine-commerce design, but seller approval, South African availability and final settlement route are not yet proven for this owner.",
    ),
    "algora": MarketPriority(
        12, "OPPORTUNISTIC", "PROVE_SOLVER_MUTATION_AND_PAYOUT", 2, 3, 4, "MEDIUM",
        "Bounty reward after accepted claim/PR using the platform payout route",
        "Individual bounty wins can be valuable, but solver mutation and payout automation are not yet strong enough for the first autonomous lane.",
    ),
    "opire": MarketPriority(
        13, "OPPORTUNISTIC", "KEEP_MANUAL_UNTIL_SOLVER_API_EXISTS", 1, 3, 4, "MEDIUM",
        "Reward payout after creator approval",
        "Useful coding-bounty upside, but no verified public solver mutation contract and non-escrow reward flow make it unsuitable for unattended acquisition today.",
    ),
    "swarms": MarketPriority(
        14, "BACKLOG", "VERIFY_PUBLISHING_AND_PAYOUT", 2, 2, 4, "LOW",
        "Unverified marketplace payout route",
        "Potential reusable-agent sales are attractive, but publishing contract, payout route and licence constraints need proof first.",
    ),
    "hyrve": MarketPriority(
        15, "BACKLOG", "VERIFY_API_AND_SA_PAYOUT", 2, 2, 3, "LOW",
        "Claimed Stripe/bank withdrawal route requiring account proof",
        "Potential work and service sales exist, but public automation and South African payout are still unverified.",
    ),
    "agentverserun": MarketPriority(
        16, "BACKLOG", "VERIFY_REAL_AUTOMATION_AND_PAYOUT", 2, 2, 3, "LOW",
        "Claimed Stripe settlement requiring account proof",
        "Storefront/project potential remains secondary until the public automation contract and payout route are demonstrated.",
    ),
    "virtuals-acp": MarketPriority(
        17, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 5, "LOW",
        "Off-host on-chain settlement only",
        "Potentially high autonomous agent-commerce ceiling, but requires a separate compliant settlement/runtime bridge and stronger revenue proof.",
    ),
    "olas-mech": MarketPriority(
        18, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 4, "LOW",
        "Off-host on-chain settlement only",
        "Agent-call monetisation is conceptually strong, but it is infrastructure-heavier and less immediate than proven fiat/PayPal channels.",
    ),
    "masumi-sokosumi": MarketPriority(
        19, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 4, "LOW",
        "Off-host on-chain settlement only",
        "Keep as a future agent-to-agent commerce lane after external wallet/CASP settlement is operational.",
    ),
    "singularitynet": MarketPriority(
        20, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 4, "LOW",
        "Off-host on-chain settlement only",
        "Potential service marketplace revenue, but not worth delaying the simpler South-African-payable channels.",
    ),
    "fetch-agentverse": MarketPriority(
        21, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 4, "LOW",
        "Off-host blockchain settlement only",
        "Keep the blockchain path external and revisit after the core treasury bridge is proven.",
    ),
    "okx-ai": MarketPriority(
        22, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 3, "LOW",
        "Off-host wallet settlement only",
        "Potential agent ecosystem income is currently less proven than the higher-ranked machine-commerce routes.",
    ),
    "agrenting": MarketPriority(
        23, "BUILD_OFFHOST", "RESEARCH_AFTER_CORE_CHANNELS", 5, 2, 3, "LOW",
        "Off-host wallet/on-chain settlement only",
        "Do not spend core implementation time here until real demand and the external settlement bridge are proven.",
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
        "Do not present test/internal credits as an earning market until cash redemption and settlement are independently proven.",
    ),
    "chowdr": MarketPriority(
        27, "ARCHIVE", "REMOVE_FROM_ACTIVE_DASHBOARD", 0, 1, 0, "LOW",
        "Beta/test credits; real money not proven",
        "Do not present beta credits as an earning market until a real-money payout route exists.",
    ),
}


CANONICAL_EARNING_MARKETS = frozenset(set(LEGACY_MARKETS) | set(REVENUE_MARKETS))
ARCHIVED_MARKETS = frozenset(slug for slug, priority in PRIORITIES.items() if priority.tier == "ARCHIVE")
ACTIVE_MARKETS = frozenset(CANONICAL_EARNING_MARKETS - ARCHIVED_MARKETS)

if set(PRIORITIES) != CANONICAL_EARNING_MARKETS:
    missing = sorted(CANONICAL_EARNING_MARKETS - set(PRIORITIES))
    extra = sorted(set(PRIORITIES) - CANONICAL_EARNING_MARKETS)
    raise RuntimeError(f"market priority coverage mismatch missing={missing} extra={extra}")


def priority_for(market_slug: str) -> MarketPriority:
    return PRIORITIES[market_slug]
