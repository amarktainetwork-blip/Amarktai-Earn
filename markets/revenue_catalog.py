from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
import hashlib
import json

from django.db import transaction

from control.models import MarketIntegrationProfile, Marketplace, MarketPolicyVersion


REVENUE_CHANNELS = (
    "POSTED_JOB",
    "BOUNTY",
    "SERVICE_LISTING",
    "PAY_PER_CALL_API",
    "PROJECT_HIRE",
    "SUBSCRIPTION",
    "MANUAL_STOREFRONT",
    "OFFHOST_SETTLEMENT",
)

HOSTING_POLICIES = ("WEBDOCK_SAFE", "OFFHOST_SETTLEMENT_REQUIRED", "UNVERIFIED")

SELLER_CAPABILITY_KEYS = (
    "publish_service",
    "update_service",
    "pause_service",
    "receive_orders",
    "order_status",
    "service_messages",
    "service_delivery",
    "usage_metering",
    "seller_payment",
    "seller_webhooks",
    "subscription_sales",
    "pay_per_call",
    "project_sales",
)


def _seller_capabilities(*supported: str) -> dict[str, bool]:
    supported_set = set(supported)
    return {key: key in supported_set for key in SELLER_CAPABILITY_KEYS}


@dataclass(frozen=True)
class RevenueMarketDefinition:
    slug: str
    display_name: str
    channels: tuple[str, ...]
    source_urls: tuple[str, ...]
    seller_capabilities: dict[str, bool]
    source_wired: bool
    auth_method: str
    job_acquisition_mode: str
    seller_mode: str
    settlement_rail: str
    currency: str
    hosting_policy: str
    api_contract_state: str
    payout_proof_state: str
    manual_onboarding_required: bool
    blockers: tuple[str, ...]
    evidence_notes: str
    adapter_name: str = "MANUAL_OR_FUTURE_CONTRACT"


WEBDOCK_DEFINITIONS = (
    RevenueMarketDefinition(
        slug="nevermined",
        display_name="Nevermined",
        channels=("SERVICE_LISTING", "PAY_PER_CALL_API", "PROJECT_HIRE", "SUBSCRIPTION"),
        source_urls=(
            "https://docs.nevermined.app/docs/getting-started/quickstart",
            "https://docs.nevermined.app/docs/development-guide/registration",
            "https://nevermined.ai/docs/getting-started/faq",
        ),
        seller_capabilities=_seller_capabilities(
            "publish_service", "update_service", "receive_orders", "order_status", "service_delivery",
            "usage_metering", "seller_payment", "subscription_sales", "pay_per_call", "project_sales",
        ),
        source_wired=True,
        auth_method="Nevermined API key; Stripe Connect account evidence required for fiat",
        job_acquisition_mode="NONE",
        seller_mode="HTTP/MCP/A2A service and payment-plan registration",
        settlement_rail="FIAT_STRIPE_CONNECT_ONLY",
        currency="USD",
        hosting_policy="WEBDOCK_SAFE",
        api_contract_state="OFFICIAL_CONTRACT_LOCALLY_VALIDATED",
        payout_proof_state="EXTERNAL_PROOF_REQUIRED",
        manual_onboarding_required=True,
        blockers=(
            "ACCOUNT_NOT_CONFIGURED", "STRIPE_CONNECT_NOT_VERIFIED", "SOUTH_AFRICA_PAYOUT_NOT_VERIFIED",
            "FIAT_PLAN_NOT_VERIFIED", "SERVICE_REGISTRATION_NOT_PROVEN",
        ),
        evidence_notes="Official docs support HTTP/MCP/A2A registration, request/time plans, and fiat payouts through Stripe Connect. Webdock rejects crypto price types.",
        adapter_name="control.services.seller_protocols.NeverminedFiatContract",
    ),
    RevenueMarketDefinition(
        slug="skyfire",
        display_name="Skyfire",
        channels=("SERVICE_LISTING", "PAY_PER_CALL_API"),
        source_urls=(
            "https://docs.skyfire.xyz/docs/seller-onboarding",
            "https://docs.skyfire.xyz/docs/token-schemas",
            "https://docs.skyfire.xyz/reference/settlement-of-payments",
        ),
        seller_capabilities=_seller_capabilities(
            "publish_service", "receive_orders", "service_delivery", "usage_metering", "seller_payment", "pay_per_call",
        ),
        source_wired=True,
        auth_method="Seller API key plus issuer JWKS token verification",
        job_acquisition_mode="NONE",
        seller_mode="Approved API/OpenAPI/MCP seller service",
        settlement_rail="CARD_OR_BANK_ONLY_UNVERIFIED",
        currency="USD",
        hosting_policy="UNVERIFIED",
        api_contract_state="OFFICIAL_TOKEN_CONTRACT_LOCALLY_VALIDATED",
        payout_proof_state="NON_CRYPTO_EXTERNAL_PROOF_REQUIRED",
        manual_onboarding_required=True,
        blockers=(
            "ACCOUNT_NOT_CONFIGURED", "SELLER_KYA_NOT_VERIFIED", "SERVICE_APPROVAL_NOT_VERIFIED",
            "SOUTH_AFRICA_PAYOUT_NOT_VERIFIED", "NON_CRYPTO_SETTLEMENT_NOT_VERIFIED",
        ),
        evidence_notes="Official tokens can declare COIN, CARD, or BANK settlement. Webdock permits only separately proven non-crypto settlement.",
        adapter_name="control.services.seller_protocols.SkyfireSellerContract",
    ),
    RevenueMarketDefinition(
        slug="hyrve",
        display_name="HYRVE",
        channels=("POSTED_JOB", "SERVICE_LISTING", "PROJECT_HIRE"),
        source_urls=("https://hyrveai.com/", "https://hyrveai.com/terms"),
        seller_capabilities=_seller_capabilities("project_sales"),
        source_wired=False,
        auth_method="Public programmatic contract not verified",
        job_acquisition_mode="MANUAL_ONLY",
        seller_mode="Manual agent/service onboarding",
        settlement_rail="STRIPE_BANK_WITHDRAWAL_CLAIMED_NOT_ACCOUNT_PROVEN",
        currency="USD",
        hosting_policy="UNVERIFIED",
        api_contract_state="PUBLIC_API_CONTRACT_NOT_VERIFIED",
        payout_proof_state="EXTERNAL_PROOF_REQUIRED",
        manual_onboarding_required=True,
        blockers=(
            "PUBLIC_API_CONTRACT_NOT_VERIFIED", "ACCOUNT_NOT_CONFIGURED", "BANK_PAYOUT_NOT_VERIFIED",
            "SOUTH_AFRICA_PAYOUT_NOT_VERIFIED",
        ),
        evidence_notes="Official public pages describe services, jobs, Stripe, bank withdrawal, escrow, and A2A commerce; no mutation adapter is created without a proven public contract.",
    ),
    RevenueMarketDefinition(
        slug="swarms",
        display_name="Swarms Marketplace",
        channels=("SERVICE_LISTING",),
        source_urls=(
            "https://docs.swarms.world/examples/overviews/basic-overview",
            "https://docs.swarms.world/api/agent",
        ),
        seller_capabilities=_seller_capabilities("publish_service"),
        source_wired=False,
        auth_method="Publishing contract and payout route require external proof",
        job_acquisition_mode="NONE",
        seller_mode="Agent/tool/prompt distribution only",
        settlement_rail="UNVERIFIED",
        currency="USD",
        hosting_policy="UNVERIFIED",
        api_contract_state="DOCUMENTED_DISTRIBUTION_NO_MUTATION_ADAPTER",
        payout_proof_state="UNVERIFIED",
        manual_onboarding_required=True,
        blockers=("PUBLISHING_CONTRACT_NOT_LOCALLY_PROVEN", "PAYOUT_ROUTE_NOT_VERIFIED", "LICENSE_REVIEW_REQUIRED"),
        evidence_notes="Official docs show marketplace publishing/loading, not a proven continuous posted-labor feed or verified cash payout route.",
    ),
    RevenueMarketDefinition(
        slug="agentverserun",
        display_name="AgentVerse.run",
        channels=("MANUAL_STOREFRONT", "PROJECT_HIRE", "SUBSCRIPTION"),
        source_urls=("https://www.agentverse.run/about",),
        seller_capabilities=_seller_capabilities("project_sales", "subscription_sales"),
        source_wired=False,
        auth_method="Dashboard/manual only; public automation contract not verified",
        job_acquisition_mode="MANUAL_ONLY",
        seller_mode="Manual storefront and external proof",
        settlement_rail="STRIPE_CLAIMED_NOT_ACCOUNT_PROVEN",
        currency="USD",
        hosting_policy="UNVERIFIED",
        api_contract_state="PUBLIC_API_CONTRACT_NOT_VERIFIED",
        payout_proof_state="UNVERIFIED",
        manual_onboarding_required=True,
        blockers=("PUBLIC_API_CONTRACT_NOT_VERIFIED", "ACCOUNT_NOT_CONFIGURED", "PAYOUT_ROUTE_NOT_VERIFIED", "SOUTH_AFRICA_PAYOUT_NOT_VERIFIED"),
        evidence_notes="Official site describes marketplace publishing, subscriptions, project hires, and Stripe billing; programmatic lifecycle is not proven.",
    ),
    RevenueMarketDefinition(
        slug="agentmarket",
        display_name="AgentMarket Candidate",
        channels=("MANUAL_STOREFRONT",),
        source_urls=("https://www.agentmarket.space/",),
        seller_capabilities=_seller_capabilities(), source_wired=False,
        auth_method="UNVERIFIED", job_acquisition_mode="MONITOR_ONLY", seller_mode="CANDIDATE",
        settlement_rail="INTERNAL_CREDITS_NOT_CASH", currency="USD", hosting_policy="UNVERIFIED",
        api_contract_state="UNVERIFIED", payout_proof_state="CREDITS_NOT_REDEEMABLE_CASH_PROOF",
        manual_onboarding_required=True,
        blockers=("CREDIT_REDEMPTION_NOT_VERIFIED", "PAYOUT_ROUTE_NOT_VERIFIED"),
        evidence_notes="Internal credits must never enter revenue, payout, profit, or settled-cash accounting without withdrawal proof.",
    ),
    RevenueMarketDefinition(
        slug="chowdr",
        display_name="Chowdr Candidate",
        channels=("MANUAL_STOREFRONT",),
        source_urls=("https://chowdr.net/",),
        seller_capabilities=_seller_capabilities(), source_wired=False,
        auth_method="UNVERIFIED", job_acquisition_mode="MONITOR_ONLY", seller_mode="CANDIDATE",
        settlement_rail="TEST_CREDITS_NOT_CASH", currency="USD", hosting_policy="UNVERIFIED",
        api_contract_state="BETA_UNVERIFIED", payout_proof_state="REAL_MONEY_NOT_PROVEN",
        manual_onboarding_required=True,
        blockers=("BETA_TEST_CREDITS_ONLY", "REAL_MONEY_NOT_PROVEN"),
        evidence_notes="Beta/test credits are non-cash and excluded from economic accounting.",
    ),
)


_OFFHOST = (
    ("virtuals-acp", "Virtuals ACP", "https://whitepaper.virtuals.io/"),
    ("coinbase-x402-bazaar", "Coinbase x402 Bazaar", "https://docs.cdp.coinbase.com/x402/"),
    ("okx-ai", "OKX AI", "https://www.okx.com/web3"),
    ("agrenting", "Agrenting", "https://agrenting.ai/"),
    ("olas-mech", "Olas Mech", "https://docs.olas.network/"),
    ("masumi-sokosumi", "Masumi / Sokosumi", "https://docs.masumi.network/"),
    ("singularitynet", "SingularityNET", "https://dev.singularitynet.io/"),
    ("fetch-agentverse", "Fetch Agentverse (blockchain path)", "https://docs.agentverse.ai/"),
    ("clawrr", "Clawrr", "https://clawrr.com/"),
    ("planetloga", "PlanetLoga", "https://planetloga.com/"),
)

OFFHOST_DEFINITIONS = tuple(
    RevenueMarketDefinition(
        slug=slug, display_name=name, channels=("OFFHOST_SETTLEMENT",), source_urls=(url,),
        seller_capabilities=_seller_capabilities(), source_wired=False,
        auth_method="NO WEBDOCK CREDENTIALS", job_acquisition_mode="NONE", seller_mode="FUTURE_EXTERNAL_BRIDGE_ONLY",
        settlement_rail="CRYPTO_OR_ONCHAIN_OFFHOST_ONLY", currency="USD",
        hosting_policy="OFFHOST_SETTLEMENT_REQUIRED", api_contract_state="FUTURE_REVIEW_REQUIRED",
        payout_proof_state="PROHIBITED_ON_WEBDOCK", manual_onboarding_required=True,
        blockers=("OFFHOST_SETTLEMENT_REQUIRED", "WALLET_EXECUTION_PROHIBITED_ON_WEBDOCK", "EXTERNAL_SETTLEMENT_BRIDGE_NOT_IMPLEMENTED"),
        evidence_notes="Candidate is catalogued only; no wallet, key, chain, testnet, or transaction code is initialized on Webdock.",
    ) for slug, name, url in _OFFHOST
)

DEFINITIONS = WEBDOCK_DEFINITIONS + OFFHOST_DEFINITIONS
BY_SLUG = {definition.slug: definition for definition in DEFINITIONS}
POLICY_CHECKED_AT = datetime(2026, 8, 9, 12, 0, tzinfo=datetime_timezone.utc)


def policy_hash(definition: RevenueMarketDefinition) -> str:
    payload = {
        "sources": definition.source_urls,
        "channels": definition.channels,
        "seller_capabilities": definition.seller_capabilities,
        "hosting_policy": definition.hosting_policy,
        "api_contract_state": definition.api_contract_state,
        "settlement_rail": definition.settlement_rail,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@transaction.atomic
def bootstrap_revenue_market_catalog() -> dict[str, int]:
    created = updated = 0
    checked_at = POLICY_CHECKED_AT
    for definition in DEFINITIONS:
        market, was_created = Marketplace.objects.get_or_create(
            slug=definition.slug,
            defaults={
                "display_name": definition.display_name,
                "status": Marketplace.Status.PAYOUT_BLOCKED,
                "enabled": False,
                "payout_ready": False,
                "south_africa_verified": False,
                "payment_model": definition.settlement_rail,
            },
        )
        created += int(was_created)
        _, profile_created = MarketIntegrationProfile.objects.get_or_create(
            marketplace=market,
            defaults={
                "adapter_name": definition.adapter_name,
                "adapter_version": "v1",
                "source_wired": definition.source_wired,
                "autonomous_acquisition_enabled": False,
                "policy_verified": False,
                "docs_checked_at": checked_at,
                "auth_method": definition.auth_method,
                "rate_limit": "No live calls; future adapter must apply documented limits and retry policy",
                "payout_method": definition.settlement_rail,
                "capabilities": {},
                "source_urls": list(definition.source_urls),
                "blockers": list(definition.blockers),
                "evidence": {"notes": definition.evidence_notes, "cash_accounting": "ONLY_AUTHORITATIVE_SETTLED_PAYOUT_IS_CASH"},
                "revenue_channels": list(definition.channels),
                "seller_capabilities": definition.seller_capabilities,
                "automation_status": "BLOCKED",
                "job_acquisition_mode": definition.job_acquisition_mode,
                "seller_mode": definition.seller_mode,
                "settlement_rail": definition.settlement_rail,
                "currency": definition.currency,
                "hosting_policy": definition.hosting_policy,
                "api_contract_state": definition.api_contract_state,
                "payout_proof_state": definition.payout_proof_state,
                "manual_onboarding_required": definition.manual_onboarding_required,
            },
        )
        updated += 0
        digest = policy_hash(definition)
        MarketPolicyVersion.objects.update_or_create(
            marketplace=market,
            policy_hash=digest,
            defaults={
                "source_url": definition.source_urls[0],
                "automation_allowed": False,
                "webdock_compatible": definition.hosting_policy == "WEBDOCK_SAFE",
                "checked_at": checked_at,
                "snapshot": {
                    "channels": list(definition.channels),
                    "seller_capabilities": definition.seller_capabilities,
                    "hosting_policy": definition.hosting_policy,
                    "settlement_rail": definition.settlement_rail,
                    "blockers": list(definition.blockers),
                },
            },
        )
    return {"created": created, "updated": updated, "total": len(DEFINITIONS)}
