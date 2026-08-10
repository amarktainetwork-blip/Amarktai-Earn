from __future__ import annotations

from control.channel_launch_rules import PRIORITY_CHANNEL_SPECS, build_channel_launch_plan
from control.models import Marketplace
from control.services.settlement_routes import settlement_routes_snapshot


def priority_channel_launch_snapshot() -> dict:
    route_rows = {row["market"]: row for row in settlement_routes_snapshot()["rows"]}
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
        plans.append(build_channel_launch_plan(
            slug=slug,
            market=market_payload,
            profile=profile_payload,
            route=route_rows.get(slug),
        ))
    return {
        "section": "priority-channel-launch",
        "rows": plans,
        "meta": {
            "priority_channels": list(PRIORITY_CHANNEL_SPECS),
            "shadow_preparation_ready": sum(1 for row in plans if row["shadow_preparation_ready"]),
            "activation_ready": sum(1 for row in plans if row["activation_ready"]),
            "external_mutation_allowed": False,
            "truth": "Preparation is local and reversible. No account creation, listing publication, checkout activation, payout mutation or paid external execution is performed by this snapshot.",
        },
    }
