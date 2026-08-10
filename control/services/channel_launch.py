from __future__ import annotations

from control.channel_launch_rules import PRIORITY_CHANNEL_SPECS, build_channel_launch_plan
from control.models import Marketplace
from control.services.channel_packages import priority_channel_package_snapshot
from control.services.settlement_routes import settlement_routes_snapshot


def priority_channel_launch_snapshot() -> dict:
    route_rows = {row["market"]: row for row in settlement_routes_snapshot()["rows"]}
    package_snapshot = priority_channel_package_snapshot()
    packages_by_market: dict[str, list[dict]] = {slug: [] for slug in PRIORITY_CHANNEL_SPECS}
    for package in package_snapshot["rows"]:
        packages_by_market.setdefault(package["market"], []).append(package)

    plans = []
    for slug in PRIORITY_CHANNEL_SPECS:
        market = Marketplace.objects.filter(slug=slug).first()
        market_payload = None
        profile_payload = None
        if market is not None:
            market_payload = {
                "enabled": bool(market.enabled),
                "status": market.status,
                "payout_ready": bool(market.payout_ready),
                "south_africa_verified": bool(market.south_africa_verified),
            }
            try:
                profile = market.integration_profile
            except Exception:
                profile = None
            if profile is not None:
                evidence = profile.evidence if isinstance(profile.evidence, dict) else {}
                catalog_truth = evidence.get("catalog_truth") if isinstance(evidence.get("catalog_truth"), dict) else {}
                profile_payload = {
                    "hosting_policy": profile.hosting_policy,
                    "api_contract_state": profile.api_contract_state,
                    "payout_proof_state": profile.payout_proof_state,
                    "source_wired": bool(profile.source_wired),
                    "revenue_channels": list(profile.revenue_channels or []),
                    "seller_capabilities": dict(profile.seller_capabilities or {}),
                    "blockers": list(profile.blockers or []),
                    "catalog_truth": dict(catalog_truth),
                }
        plan = build_channel_launch_plan(
            slug=slug,
            market=market_payload,
            profile=profile_payload,
            route=route_rows.get(slug),
        )
        packages = packages_by_market.get(slug, [])
        plan["packages"] = packages
        plan["package_count"] = len(packages)
        plan["packages_prepared"] = sum(1 for row in packages if row["prepared"])
        plan["packages_price_ready"] = sum(1 for row in packages if row["price_ready"])
        plans.append(plan)
    return {
        "section": "priority-channel-launch",
        "rows": plans,
        "meta": {
            "priority_channels": list(PRIORITY_CHANNEL_SPECS),
            "shadow_preparation_ready": sum(1 for row in plans if row["shadow_preparation_ready"]),
            "activation_ready": sum(1 for row in plans if row["activation_ready"]),
            "prepared_packages": package_snapshot["meta"]["prepared_packages"],
            "price_ready_packages": package_snapshot["meta"]["price_ready_packages"],
            "total_packages": package_snapshot["meta"]["total_packages"],
            "published_packages": 0,
            "external_mutation_allowed": False,
            "truth": "Preparation is local and reversible. Package manifests and draft listings may exist locally, but no account creation, listing publication, checkout activation, payout mutation or paid external execution is performed by this snapshot.",
        },
    }
