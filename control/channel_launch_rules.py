from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ChannelLaunchSpec:
    slug: str
    execution_placement: str
    actions: tuple[str, ...]


PRIORITY_CHANNEL_SPECS = {
    "contra": ChannelLaunchSpec(
        slug="contra",
        execution_placement="WEBDOCK_LIGHT",
        actions=(
            "PACKAGE_PROVEN_SERVICE_OFFERINGS",
            "PREPARE_PROJECT_AND_SUBSCRIPTION_PRICING",
            "PREPARE_PAYMENT_LINK_COPY",
            "MAP_OWNER_PAYOUT_RAIL",
        ),
    ),
    "rapidapi": ChannelLaunchSpec(
        slug="rapidapi",
        execution_placement="WEBDOCK_LIGHT",
        actions=(
            "PACKAGE_DETERMINISTIC_LOW_COST_APIS",
            "APPLY_25_PERCENT_MARKETPLACE_FEE_TO_PRICING",
            "PREPARE_USAGE_LIMITS_AND_SUBSCRIPTION_TIERS",
            "MAP_PAYPAL_PROVIDER_PAYOUT_RAIL",
        ),
    ),
    "apify-store": ChannelLaunchSpec(
        slug="apify-store",
        execution_placement="APIFY",
        actions=(
            "PACKAGE_APIFY_ACTOR_CANDIDATES",
            "KEEP_SCRAPER_HEAVY_EXECUTION_ON_APIFY",
            "MODEL_EXTERNAL_EXECUTION_COST_BEFORE_PRICING",
            "MAP_CREATOR_PAYOUT_RAIL",
        ),
    ),
    "lemon-squeezy": ChannelLaunchSpec(
        slug="lemon-squeezy",
        execution_placement="WEBDOCK_LIGHT",
        actions=(
            "PACKAGE_DIRECT_PRODUCTS_AND_SERVICES",
            "PREPARE_ONE_TIME_SUBSCRIPTION_AND_USAGE_PRICING",
            "PREPARE_SIGNED_WEBHOOK_CONTRACT",
            "MAP_BANK_PAYOUT_RAIL",
        ),
    ),
}


def build_channel_launch_plan(
    *,
    slug: str,
    market: Mapping | None,
    profile: Mapping | None,
    route: Mapping | None,
) -> dict:
    spec = PRIORITY_CHANNEL_SPECS[slug]
    internal_blockers: list[str] = []
    external_blockers: list[str] = []

    if not isinstance(market, Mapping):
        internal_blockers.append("MARKET_CATALOG_MISSING")
    if not isinstance(profile, Mapping):
        internal_blockers.append("MARKET_INTEGRATION_PROFILE_MISSING")
    else:
        hosting = str(profile.get("hosting_policy") or "UNVERIFIED")
        evidence = profile.get("catalog_truth") if isinstance(profile.get("catalog_truth"), Mapping) else {}
        placement = str(evidence.get("execution_placement") or "UNVERIFIED")
        if hosting == "OFFHOST_SETTLEMENT_REQUIRED":
            internal_blockers.append("WEBDOCK_EXECUTION_NOT_ALLOWED")
        elif hosting != "WEBDOCK_SAFE":
            internal_blockers.append("WEBDOCK_HOSTING_POLICY_UNVERIFIED")
        if placement != spec.execution_placement:
            internal_blockers.append("EXECUTION_PLACEMENT_NOT_PROVEN")
        external_blockers.extend(str(item) for item in (profile.get("blockers") or []))

    if not isinstance(route, Mapping) or route.get("ready") is not True:
        external_blockers.append("VERIFIED_OWNER_SETTLEMENT_ROUTE_REQUIRED")
        if isinstance(route, Mapping):
            external_blockers.extend(str(item) for item in (route.get("blockers") or []))

    if isinstance(market, Mapping):
        if market.get("payout_ready") is not True:
            external_blockers.append("MARKET_PAYOUT_NOT_READY")
        if market.get("south_africa_verified") is not True:
            external_blockers.append("SOUTH_AFRICA_PAYOUT_NOT_VERIFIED")

    internal_blockers = list(dict.fromkeys(internal_blockers))
    external_blockers = list(dict.fromkeys(external_blockers))
    return {
        "market": slug,
        "execution_placement": spec.execution_placement,
        "shadow_preparation_ready": not internal_blockers,
        "activation_ready": not internal_blockers and not external_blockers,
        "external_mutation_allowed": False,
        "actions": list(spec.actions),
        "internal_blockers": internal_blockers,
        "external_blockers": external_blockers,
        "truth": "Shadow preparation may proceed without external mutation. Publishing, onboarding, KYC, payment activation and payout-route changes remain manual until separately proven.",
    }
