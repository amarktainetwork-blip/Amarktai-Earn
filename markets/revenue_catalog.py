from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone as datetime_timezone
import hashlib
import json

from django.db import transaction

from control.models import MarketIntegrationProfile, Marketplace, MarketPolicyVersion
from markets.catalog import BY_SLUG as LEGACY_BY_SLUG


REVENUE_CHANNELS = (
    "POSTED_JOB",
    "BOUNTY",
    "SERVICE_LISTING",
    "PAY_PER_CALL_API",
    "PROJECT_HIRE",
    "SUBSCRIPTION",
    "MANUAL_STOREFRONT",
    "DIRECT_CHECKOUT",
    "PAYMENT_LINK",
    "OFFHOST_SETTLEMENT",
)

HOSTING_POLICIES = ("WEBDOCK_SAFE", "OFFHOST_SETTLEMENT_REQUIRED", "UNVERIFIED")
EXECUTION_PLACEMENTS = ("WEBDOCK_LIGHT", "EXTERNAL_PROVIDER", "APIFY", "MANUAL", "OFFHOST_REQUIRED", "UNVERIFIED")

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
    economics: dict[str, object] = field(default_factory=dict)
    execution_placement: str = "UNVERIFIED"


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
        execution_placement="WEBDOCK_LIGHT",
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
        execution_placement="UNVERIFIED",
    ),
    RevenueMarketDefinition(
        slug="contra",
        display_name="Contra",
        channels=("SERVICE_LISTING", "PROJECT_HIRE", "SUBSCRIPTION", "PAYMENT_LINK"),
        source_urls=(
            "https://contra.com/",
            "https://help.contra.com/en/articles/10008642-payouts",
            "https://help.contra.com/en/articles/13655628-payment-links",
        ),
        seller_capabilities=_seller_capabilities("project_sales", "subscription_sales"),
        source_wired=False,
        auth_method="Owner account and identity verification; no public mutation credential configured",
        job_acquisition_mode="MANUAL_ONLY",
        seller_mode="MANUAL_STOREFRONT_PROJECTS_AND_PAYMENT_LINKS",
        settlement_rail="ACCOUNT_SPECIFIC_LOCAL_BANK_PAYPAL_OR_PAYONEER_PROOF_REQUIRED",
        currency="USD",
        hosting_policy="WEBDOCK_SAFE",
        api_contract_state="PUBLIC_AUTOMATION_CONTRACT_NOT_VERIFIED",
        payout_proof_state="SOUTH_AFRICA_ACCOUNT_OPTIONS_NOT_PROVEN",
        manual_onboarding_required=True,
        blockers=(
            "ACCOUNT_NOT_CONFIGURED", "IDENTITY_NOT_VERIFIED", "SOUTH_AFRICA_PAYOUT_OPTIONS_NOT_PROVEN",
            "PUBLIC_AUTOMATION_CONTRACT_NOT_VERIFIED", "FIRST_SERVICE_LISTING_NOT_PROVEN",
        ),
        evidence_notes="Phase 1 shadow candidate. Projects, invoices/payment links and multiple payout methods are relevant, but this owner's South African payout options and automation contract must be proven before activation.",
        economics={"verified": False, "settlement_delay_days": None},
        execution_placement="WEBDOCK_LIGHT",
    ),
    RevenueMarketDefinition(
        slug="rapidapi",
        display_name="RapidAPI",
        channels=("PAY_PER_CALL_API", "SUBSCRIPTION"),
        source_urls=("https://rapidapi.com/", "https://docs.rapidapi.com/"),
        seller_capabilities=_seller_capabilities("pay_per_call", "subscription_sales"),
        source_wired=False,
        auth_method="Provider account plus API listing/onboarding; no auto-publish credential configured",
        job_acquisition_mode="NONE",
        seller_mode="API_MARKETPLACE_PROVIDER",
        settlement_rail="PAYPAL_PROVIDER_PAYOUT_ACCOUNT_PROOF_REQUIRED",
        currency="USD",
        hosting_policy="WEBDOCK_SAFE",
        api_contract_state="RUNTIME_API_COMPATIBLE_PUBLISHING_AUTOMATION_NOT_PROVEN",
        payout_proof_state="PAYPAL_AND_SOUTH_AFRICA_WITHDRAWAL_PROOF_REQUIRED",
        manual_onboarding_required=True,
        blockers=(
            "PROVIDER_ACCOUNT_NOT_CONFIGURED", "API_LISTING_NOT_PUBLISHED", "PAYPAL_PAYOUT_NOT_VERIFIED",
            "SOUTH_AFRICA_WITHDRAWAL_NOT_VERIFIED", "PUBLISHING_AUTOMATION_NOT_VERIFIED",
        ),
        evidence_notes="Phase 1 shadow candidate for deterministic low-cost APIs. Current provider fee evidence is represented conservatively; payout/PayPal processing remains account-dependent and blocks LIVE state.",
        economics={
            "verified": False,
            "percentage_fee_rate": "0.25",
            "fixed_transaction_fee": "0",
            "payout_cost_rate": None,
            "fx_cost_rate": None,
            "chargeback_reserve_rate": "0",
            "settlement_delay_days": None,
        },
        execution_placement="WEBDOCK_LIGHT",
    ),
    RevenueMarketDefinition(
        slug="apify-store",
        display_name="Apify Store",
        channels=("SERVICE_LISTING", "PAY_PER_CALL_API"),
        source_urls=(
            "https://docs.apify.com/actors/publishing/monetize/pay-per-event",
            "https://docs.apify.com/legal/store-publishing-terms-and-conditions",
        ),
        seller_capabilities=_seller_capabilities("pay_per_call", "usage_metering"),
        source_wired=False,
        auth_method="Apify creator account/KYC; Actor publishing remains external to Webdock",
        job_acquisition_mode="NONE",
        seller_mode="APIFY_ACTOR_STORE_PPE",
        settlement_rail="APIFY_CREATOR_PAYOUT_ACCOUNT_PROOF_REQUIRED",
        currency="USD",
        hosting_policy="WEBDOCK_SAFE",
        api_contract_state="ACTOR_RUNTIME_EXTERNAL_PUBLISHING_NOT_LOCALLY_WIRED",
        payout_proof_state="CREATOR_KYC_AND_PAYOUT_NOT_PROVEN",
        manual_onboarding_required=True,
        blockers=(
            "CREATOR_ACCOUNT_NOT_CONFIGURED", "KYC_NOT_VERIFIED", "PAYOUT_ROUTE_NOT_VERIFIED",
            "ACTOR_NOT_PUBLISHED", "EXTERNAL_EXECUTION_COST_PROFILE_NOT_PROVEN",
        ),
        evidence_notes="Phase 1 shadow candidate. Scraper-heavy execution belongs on Apify infrastructure; Webdock remains the orchestration/economics/control plane and must not run continuous high-load third-party scraping.",
        economics={
            "verified": False,
            "percentage_fee_rate": "0.20",
            "fixed_transaction_fee": "0",
            "payout_cost_rate": None,
            "fx_cost_rate": None,
            "chargeback_reserve_rate": "0",
            "external_execution_cost_usd": None,
            "settlement_delay_days": None,
        },
        execution_placement="APIFY",
    ),
    RevenueMarketDefinition(
        slug="lemon-squeezy",
        display_name="Lemon Squeezy Direct",
        channels=("DIRECT_CHECKOUT", "SUBSCRIPTION", "PAYMENT_LINK", "PAY_PER_CALL_API"),
        source_urls=(
            "https://docs.lemonsqueezy.com/help/getting-started/supported-countries",
            "https://docs.lemonsqueezy.com/help/products/pricing-models",
            "https://docs.lemonsqueezy.com/api",
        ),
        seller_capabilities=_seller_capabilities("receive_orders", "order_status", "seller_webhooks", "subscription_sales", "pay_per_call"),
        source_wired=False,
        auth_method="Merchant account, API key and signed webhooks after owner onboarding",
        job_acquisition_mode="NONE",
        seller_mode="DIRECT_CHECKOUT_SUBSCRIPTIONS_AND_USAGE_BILLING",
        settlement_rail="SOUTH_AFRICA_BANK_PAYOUT_SUPPORTED_ACCOUNT_PROOF_REQUIRED",
        currency="USD",
        hosting_policy="WEBDOCK_SAFE",
        api_contract_state="OFFICIAL_API_AND_WEBHOOK_CONTRACT_NOT_LOCALLY_WIRED",
        payout_proof_state="OWNER_ACCOUNT_KYC_AND_BANK_PAYOUT_NOT_PROVEN",
        manual_onboarding_required=True,
        blockers=(
            "MERCHANT_ACCOUNT_NOT_CONFIGURED", "KYC_NOT_VERIFIED", "BANK_PAYOUT_ACCOUNT_NOT_PROVEN",
            "API_WEBHOOK_NOT_WIRED", "FIRST_PRODUCT_NOT_PUBLISHED",
        ),
        evidence_notes="Phase 1 shadow direct-commerce candidate. It can support one-time, subscription and usage-style products, but account/KYC/bank payout and signed webhook execution remain unproven locally.",
        economics={
            "verified": False,
            "percentage_fee_rate": "0.05",
            "fixed_transaction_fee": "0.50",
            "payout_cost_rate": None,
            "fx_cost_rate": None,
            "chargeback_reserve_rate": None,
            "settlement_delay_days": None,
        },
        execution_placement="WEBDOCK_LIGHT",
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
        execution_placement="OFFHOST_REQUIRED",
    ) for slug, name, url in _OFFHOST
)

DEFINITIONS = WEBDOCK_DEFINITIONS + OFFHOST_DEFINITIONS
BY_SLUG = {definition.slug: definition for definition in DEFINITIONS}
POLICY_CHECKED_AT = datetime(2026, 8, 9, 12, 0, tzinfo=datetime_timezone.utc)

LEGACY_PROFILE_ENRICHMENTS = {
    "agentgigs": {
        "revenue_channels": ["POSTED_JOB"], "job_acquisition_mode": "REST_API",
        "seller_mode": "NONE_VERIFIED", "settlement_rail": "STRIPE_CONNECT_ACCOUNT_PROOF_REQUIRED",
        "hosting_policy": "WEBDOCK_SAFE", "api_contract_state": "OFFICIAL_REST_CONTRACT",
    },
    "dealwork": {
        "revenue_channels": ["POSTED_JOB"], "job_acquisition_mode": "REST_API",
        "seller_mode": "SERVICE_LISTING_CONTRACT_UNVERIFIED", "settlement_rail": "WALLET_WITHDRAWAL_RAIL_UNVERIFIED",
        "hosting_policy": "WEBDOCK_SAFE", "api_contract_state": "OFFICIAL_REST_CONTRACT",
    },
    "callboard": {
        "revenue_channels": ["POSTED_JOB"], "job_acquisition_mode": "OPENAPI_V2",
        "seller_mode": "NONE_VERIFIED", "settlement_rail": "STRIPE_CONNECT_ACCOUNT_PROOF_REQUIRED",
        "hosting_policy": "WEBDOCK_SAFE", "api_contract_state": "OFFICIAL_OPENAPI_CONTRACT",
    },
    "taskbounty": {
        "revenue_channels": ["BOUNTY"], "job_acquisition_mode": "REST_API",
        "seller_mode": "NONE_VERIFIED", "settlement_rail": "USD_BANK_TRANSFER_ONLY",
        "hosting_policy": "WEBDOCK_SAFE", "api_contract_state": "OFFICIAL_REST_CONTRACT",
    },
    "opire": {
        "revenue_channels": ["BOUNTY"], "job_acquisition_mode": "SOURCE_WIRED_IMPORT_MANUAL_WORKFLOW",
        "seller_mode": "NO_SOLVER_MUTATION_CONTRACT", "settlement_rail": "STRIPE_REWARD_CREATOR_PAYMENT_UNVERIFIED",
        "hosting_policy": "WEBDOCK_SAFE", "api_contract_state": "SOURCE_IMPORT_ONLY_NO_SOLVER_MUTATION",
    },
    "algora": {
        "revenue_channels": ["BOUNTY"], "job_acquisition_mode": "SOURCE_WIRED_IMPORT",
        "seller_mode": "SOLVER_MUTATION_CONTRACT_UNVERIFIED", "settlement_rail": "PAYOUT_ONBOARDING_RAIL_UNVERIFIED",
        "hosting_policy": "WEBDOCK_SAFE", "api_contract_state": "SOURCE_IMPORT_ONLY_SOLVER_MUTATION_UNVERIFIED",
    },
}


def policy_hash(definition: RevenueMarketDefinition) -> str:
    payload = {
        "sources": definition.source_urls,
        "channels": definition.channels,
        "seller_capabilities": definition.seller_capabilities,
        "hosting_policy": definition.hosting_policy,
        "api_contract_state": definition.api_contract_state,
        "settlement_rail": definition.settlement_rail,
        "economics": definition.economics,
        "execution_placement": definition.execution_placement,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _apply_catalog_owned_fields(profile: MarketIntegrationProfile, values: dict) -> bool:
    changed: list[str] = []
    for field_name, value in values.items():
        if getattr(profile, field_name) != value:
            setattr(profile, field_name, value)
            changed.append(field_name)
    if changed:
        profile.save(update_fields=[*changed, "updated_at"])
    return bool(changed)


def _revenue_profile_static_truth(definition: RevenueMarketDefinition) -> dict:
    catalog_evidence = {
        "notes": definition.evidence_notes,
        "cash_accounting": "ONLY_AUTHORITATIVE_SETTLED_PAYOUT_IS_CASH",
        "policy_hash": policy_hash(definition),
        "economics": dict(definition.economics),
        "execution_placement": definition.execution_placement,
    }
    return {
        "adapter_name": definition.adapter_name,
        "adapter_version": "v1",
        "source_wired": definition.source_wired,
        "docs_checked_at": POLICY_CHECKED_AT,
        "auth_method": definition.auth_method,
        "rate_limit": "No live calls; future adapter must apply documented limits and retry policy",
        "payout_method": definition.settlement_rail,
        "capabilities": {},
        "source_urls": list(definition.source_urls),
        "revenue_channels": list(definition.channels),
        "seller_capabilities": dict(definition.seller_capabilities),
        "job_acquisition_mode": definition.job_acquisition_mode,
        "seller_mode": definition.seller_mode,
        "settlement_rail": definition.settlement_rail,
        "currency": definition.currency,
        "hosting_policy": definition.hosting_policy,
        "api_contract_state": definition.api_contract_state,
        "manual_onboarding_required": definition.manual_onboarding_required,
        "evidence": {"catalog_truth": catalog_evidence},
    }


def _merge_catalog_evidence(existing, catalog_truth: dict) -> dict:
    evidence = dict(existing) if isinstance(existing, dict) else {}
    return {**evidence, "catalog_truth": dict(catalog_truth.get("catalog_truth") or {})}


@transaction.atomic
def bootstrap_revenue_market_catalog() -> dict[str, int]:
    created = profiles_created = updated = unchanged = 0
    checked_at = POLICY_CHECKED_AT
    for slug, enrichment in LEGACY_PROFILE_ENRICHMENTS.items():
        definition = LEGACY_BY_SLUG[slug]
        market, market_created = Marketplace.objects.get_or_create(
            slug=slug,
            defaults={
                "display_name": definition.display_name,
                "status": Marketplace.Status.PAYOUT_BLOCKED,
                "enabled": False,
                "payout_ready": False,
                "south_africa_verified": False,
                "payment_model": definition.payout_method,
            },
        )
        created += int(market_created)
        legacy_static = {
            **enrichment,
            "seller_capabilities": _seller_capabilities(),
            "currency": "USD",
            "manual_onboarding_required": True,
        }
        profile, profile_created = MarketIntegrationProfile.objects.get_or_create(
            marketplace=market,
            defaults={
                "adapter_name": definition.adapter_path,
                "adapter_version": "v1",
                "source_wired": True,
                "autonomous_acquisition_enabled": False,
                "policy_verified": bool(definition.capabilities.policy_verified),
                "docs_checked_at": checked_at,
                "auth_method": definition.auth_method,
                "rate_limit": definition.rate_limit,
                "payout_method": definition.payout_method,
                "capabilities": definition.capabilities.as_dict(),
                "source_urls": list(definition.source_urls),
                "blockers": list(definition.blockers),
                "evidence": definition.evidence,
                "automation_status": "BLOCKED",
                **legacy_static,
            },
        )
        profiles_created += int(profile_created)
        if not profile_created:
            if _apply_catalog_owned_fields(profile, legacy_static):
                updated += 1
            else:
                unchanged += 1
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
        static_truth = _revenue_profile_static_truth(definition)
        profile, profile_created = MarketIntegrationProfile.objects.get_or_create(
            marketplace=market,
            defaults={
                **static_truth,
                "autonomous_acquisition_enabled": False,
                "automation_status": "BLOCKED",
                "policy_verified": False,
                "blockers": list(definition.blockers),
                "payout_proof_state": definition.payout_proof_state,
            },
        )
        profiles_created += int(profile_created)
        if not profile_created:
            static_truth["evidence"] = _merge_catalog_evidence(profile.evidence, static_truth["evidence"])
            if _apply_catalog_owned_fields(profile, static_truth):
                updated += 1
            else:
                unchanged += 1
        digest = policy_hash(definition)
        MarketPolicyVersion.objects.get_or_create(
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
                    "economics": definition.economics,
                    "execution_placement": definition.execution_placement,
                },
            },
        )
    return {
        "created": created,
        "profiles_created": profiles_created,
        "updated": updated,
        "unchanged": unchanged,
        "total": len(LEGACY_PROFILE_ENRICHMENTS) + len(DEFINITIONS),
    }
