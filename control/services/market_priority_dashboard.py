from __future__ import annotations

from control.models import Marketplace
from control.services.market_control import market_control_row
from control.services.market_priority import (
    ACTIVE_MARKETS,
    ARCHIVED_MARKETS,
    CANONICAL_EARNING_MARKETS,
    PRIORITIES,
)


def _priority_payload(market_slug: str) -> dict:
    priority = PRIORITIES[market_slug]
    return {
        "priority_rank": priority.rank,
        "priority_tier": priority.tier,
        "priority_action": priority.action,
        "payout_autonomy_score": priority.payout_autonomy_score,
        "south_africa_setup_score": priority.south_africa_setup_score,
        "autonomous_earning_ceiling_score": priority.autonomous_earning_ceiling_score,
        "priority_confidence": priority.confidence,
        "priority_payout_path": priority.payout_path,
        "priority_reason": priority.priority_reason,
    }


def market_controls_snapshot() -> dict:
    """Owner-facing market control plane ordered by commercial priority.

    Only canonical real-money earning candidates are shown. Historical rows that
    are no longer in a canonical earning catalog remain in the database for
    audit/history but are not presented as markets. Explicit ARCHIVE candidates
    remain catalogued for evidence but are removed from the active control plane.
    """
    markets = list(
        Marketplace.objects.filter(slug__in=ACTIVE_MARKETS).select_related("integration_profile", "health_snapshot")
    )
    markets.sort(key=lambda market: PRIORITIES[market.slug].rank)

    rows = []
    for market in markets:
        row = market_control_row(market)
        row.update(_priority_payload(market.slug))
        rows.append(row)

    stale_non_earning_rows = Marketplace.objects.exclude(slug__in=CANONICAL_EARNING_MARKETS).count()
    archived_rows = Marketplace.objects.filter(slug__in=ARCHIVED_MARKETS).count()
    tier_counts: dict[str, int] = {}
    for row in rows:
        tier = row["priority_tier"]
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    return {
        "section": "market-controls",
        "rows": rows,
        "meta": {
            "canonical_earning_markets": len(CANONICAL_EARNING_MARKETS),
            "active_market_candidates": len(rows),
            "archived_market_candidates": archived_rows,
            "retired_non_earning_rows_hidden": stale_non_earning_rows,
            "tier_counts": tier_counts,
            "work_ready": sum(1 for row in rows if row["work_ready"]),
            "live_test_ready": sum(1 for row in rows if row["live_test_ready"]),
            "cash_ready": sum(1 for row in rows if row["cash_ready"]),
            "autonomy_ready": sum(1 for row in rows if row["autonomy_ready"]),
            "priority_truth": (
                "Priority order: automatic payout receipt first, South African owner setup second, autonomous earning ceiling third. "
                "A later human withdrawal is allowed and does not reduce earning autonomy. Unusable owner payout rails remain a hard priority penalty."
            ),
            "truth": (
                "Work readiness, live proving, payout receipt, settled cash and autonomous mutation remain independent gates. "
                "Platform-wallet or PayPal receipt never automatically means final bank-settled cash."
            ),
        },
    }
