from __future__ import annotations

from dataclasses import dataclass

from markets.agentgigs.client import AgentGigsAdapter
from markets.algora.client import AlgoraAdapter
from markets.base import MarketCapabilities
from markets.callboard.client import CallboardAdapter
from markets.dealwork.client import DealworkAdapter
from markets.gitpay.client import GitpayAdapter
from markets.opire.client import OpireAdapter
from markets.taskbounty.client import TaskBountyAdapter


@dataclass(frozen=True)
class MarketDefinition:
    slug: str
    display_name: str
    adapter_path: str
    capabilities: MarketCapabilities
    source_urls: tuple[str, ...]
    auth_method: str
    rate_limit: str
    payout_method: str
    automation_allowed: bool
    blockers: tuple[str, ...]
    evidence: dict
    webdock_compatible: bool = True


DEFINITIONS = (
    MarketDefinition(
        slug="agentgigs", display_name="AgentGigs",
        adapter_path="markets.agentgigs.client.AgentGigsAdapter", capabilities=AgentGigsAdapter.capabilities,
        source_urls=("https://www.agentgigs.io/docs/api", "https://www.agentgigs.io/terms"),
        auth_method="X-API-Key", rate_limit="30/120/300 requests per minute by tier; Retry-After honored",
        payout_method="Stripe Connect",
        automation_allowed=True,
        blockers=("ACCOUNT_PAYOUT_NOT_VERIFIED", "SOUTH_AFRICA_NON_CRYPTO_PAYOUT_NOT_VERIFIED"),
        evidence={"mode": "REST_AND_WEBHOOK", "account_proof_required": True},
        webdock_compatible=True,
    ),
    MarketDefinition(
        slug="dealwork", display_name="Dealwork",
        adapter_path="markets.dealwork.client.DealworkAdapter", capabilities=DealworkAdapter.capabilities,
        source_urls=("https://dealwork.ai/skill.md", "https://dealwork.ai/api-docs", "https://dealwork.ai/how-it-works", "https://dealwork.ai/terms"),
        auth_method="Bearer API key over MCP HTTP", rate_limit="No numeric official limit captured; remote Retry-After required",
        payout_method="Dealwork wallet withdrawal; exact rail requires account proof",
        automation_allowed=True,
        blockers=(
            "DEALWORK_KYA_NOT_VERIFIED", "WITHDRAWAL_RAIL_NOT_VERIFIED",
            "ACCOUNT_PAYOUT_NOT_VERIFIED", "SOUTH_AFRICA_NON_CRYPTO_PAYOUT_NOT_VERIFIED",
            "SERVICE_LISTING_CONTRACT_NOT_PROVED",
        ),
        evidence={"mode": "MCP_TOOLS_DISCOVERED_AT_RUNTIME", "service_listings_enabled": False},
        webdock_compatible=True,
    ),
    MarketDefinition(
        slug="callboard", display_name="Callboard",
        adapter_path="markets.callboard.client.CallboardAdapter", capabilities=CallboardAdapter.capabilities,
        source_urls=("https://getcallboard.com/docs/api-reference", "https://getcallboard.com/terms"),
        auth_method="X-API-Key with read/write scopes", rate_limit="Official API rate limiting; Retry-After honored",
        payout_method="Stripe Connect",
        automation_allowed=True,
        blockers=("AGENT_OWNER_CLAIM_NOT_VERIFIED", "ACCOUNT_PAYOUT_NOT_VERIFIED", "SOUTH_AFRICA_NON_CRYPTO_PAYOUT_NOT_VERIFIED"),
        evidence={"mode": "OPENAPI_V2", "submission_payload": "OPENAPI_SHAPED_ONLY"},
        webdock_compatible=True,
    ),
    MarketDefinition(
        slug="opire", display_name="Opire",
        adapter_path="markets.opire.client.OpireAdapter", capabilities=OpireAdapter.capabilities,
        source_urls=("https://docs.opire.dev/overview/getting-started", "https://docs.opire.dev/overview/commands"),
        auth_method="Official web/GitHub workflow; no public solver API contract verified",
        rate_limit="GitHub/platform policy; no adapter polling without an approved source reader",
        payout_method="Stripe account; reward creator pays after claim approval",
        automation_allowed=False,
        blockers=(
            "NO_DOCUMENTED_PUBLIC_SOLVER_API", "REWARD_CREATOR_PAYMENT_NOT_ESCROWED",
            "ACCOUNT_PAYOUT_NOT_VERIFIED", "SOUTH_AFRICA_NON_CRYPTO_PAYOUT_NOT_VERIFIED",
        ),
        evidence={"mode": "SOURCE_WIRED_IMPORT", "claim_is_settlement": False},
        webdock_compatible=True,
    ),
    MarketDefinition(
        slug="algora", display_name="Algora",
        adapter_path="markets.algora.client.AlgoraAdapter", capabilities=AlgoraAdapter.capabilities,
        source_urls=("https://api.docs.algora.io/", "https://algora.io/pricing/"),
        auth_method="Official bounty API/SDK; solver mutation auth not verified",
        rate_limit="No solver-side rate contract captured; no live polling until configured",
        payout_method="Algora payout onboarding; exact account rail requires proof",
        automation_allowed=False,
        blockers=(
            "SOLVER_MUTATION_AUTH_NOT_VERIFIED", "ACCOUNT_PAYOUT_NOT_VERIFIED",
            "SOUTH_AFRICA_NON_CRYPTO_PAYOUT_NOT_VERIFIED",
        ),
        evidence={"mode": "SOURCE_WIRED_IMPORT", "claim_or_merged_pr_is_settlement": False},
        webdock_compatible=True,
    ),
    MarketDefinition(
        slug="taskbounty", display_name="TaskBounty",
        adapter_path="markets.taskbounty.client.TaskBountyAdapter", capabilities=TaskBountyAdapter.capabilities,
        source_urls=(
            "https://www.task-bounty.com/for-agents/build-your-own",
            "https://www.task-bounty.com/docs/mcp", "https://www.task-bounty.com/terms",
        ),
        auth_method="Bearer tb_live API key", rate_limit="60 requests/minute; poll every 30-60 seconds",
        payout_method="USD bank transfer via owner-completed dashboard onboarding only",
        automation_allowed=True,
        blockers=("BANK_PAYOUT_ONBOARDING_REQUIRED", "ACCOUNT_PAYOUT_NOT_VERIFIED"),
        evidence={
            "mode": "REST",
            "supported_payout_selected": "USD_BANK_TRANSFER",
            "payout_configuration_method": "OWNER_DASHBOARD_ONLY",
            "crypto_payout_api_disabled": True,
            "contributor_share": "80%",
            "ai_generated_work_disclosure_required": True,
            "resubmission_only_after_failed_verification": True,
        },
        webdock_compatible=True,
    ),
    MarketDefinition(
        slug="gitpay", display_name="Gitpay",
        adapter_path="markets.gitpay.client.GitpayAdapter", capabilities=GitpayAdapter.capabilities,
        source_urls=("https://gitpay.me/", "https://github.com/worknenjoy/gitpay"),
        auth_method="First-party task source import; assignment and payment workflow remains owner-assisted until an official mutation contract is verified",
        rate_limit="Bounded source refresh only; no browser automation or generic form submission",
        payout_method="Bank or PayPal after assignment, accepted delivery, and payment request",
        automation_allowed=False,
        blockers=(
            "APPLICATION_MUTATION_CONTRACT_NOT_VERIFIED", "ASSIGNMENT_REQUIRED",
            "PAYMENT_REQUEST_OWNER_ACTION", "ACCOUNT_PAYOUT_NOT_VERIFIED",
        ),
        evidence={
            "mode": "FIRST_PARTY_SOURCE_IMPORT",
            "assignment_required_before_execution": True,
            "provider_spend_before_assignment": False,
            "supported_payout_selected": "BANK_OR_PAYPAL",
        },
        webdock_compatible=True,
    ),
)


BY_SLUG = {definition.slug: definition for definition in DEFINITIONS}
