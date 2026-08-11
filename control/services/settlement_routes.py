from __future__ import annotations

from copy import deepcopy
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, Marketplace, SystemSetting
from control.services.market_priority import ACTIVE_MARKETS, priority_for
from control.services.payment_rails import DEFAULT_PAYMENT_RAILS, payment_rail_snapshot
from control.settlement_rules import ROUTE_STATES, settlement_route_blockers


SETTLEMENT_ROUTE_SETTING_KEY = "treasury.market_settlement_routes.v1"
SETTLEMENT_ROUTE_VERSION = 2

# Candidate receipt rails only. A human may perform the final withdrawal after
# funds arrive. No South African bank account details are stored in AmarktAI.
MARKET_OWNER_RAIL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "contra": ("paypal", "payoneer", "crypto-wallet"),
    "rapidapi": ("paypal",),
    "apify-store": ("paypal", "wise"),
    "lemon-squeezy": ("paypal",),
    "nevermined": ("crypto-wallet", "valr"),
    "skyfire": ("crypto-wallet", "valr"),
    "agentgigs": (),
    "callboard": (),
    "taskbounty": ("crypto-wallet", "valr"),
    "dealwork": (),
}


def default_settlement_route_catalog() -> dict[str, Any]:
    return {"version": SETTLEMENT_ROUTE_VERSION, "routes": {}}


def _clean_text(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _merge_catalog(raw: Any) -> dict[str, Any]:
    catalog = default_settlement_route_catalog()
    if not isinstance(raw, dict) or not isinstance(raw.get("routes"), dict):
        return catalog
    for market_slug, value in raw["routes"].items():
        if not isinstance(value, dict):
            continue
        status = str(value.get("status") or "UNMAPPED").upper()
        if status not in ROUTE_STATES:
            status = "BLOCKED"
        catalog["routes"][str(market_slug)] = {
            "status": status,
            "selected_rail": _clean_text(value.get("selected_rail"), limit=80),
            "proof_reference": _clean_text(value.get("proof_reference"), limit=255),
            "notes": _clean_text(value.get("notes"), limit=1000),
            "verified_at": value.get("verified_at") or None,
        }
    return catalog


def load_settlement_route_catalog() -> dict[str, Any]:
    setting = SystemSetting.objects.filter(key=SETTLEMENT_ROUTE_SETTING_KEY).first()
    return _merge_catalog(setting.value if setting else None)


def settlement_route_row(market: Marketplace, *, catalog=None, rails_by_slug=None) -> dict[str, Any]:
    catalog = catalog or load_settlement_route_catalog()
    if rails_by_slug is None:
        rails_by_slug = {row["slug"]: row for row in payment_rail_snapshot()["rows"]}
    persisted = catalog["routes"].get(market.slug) or {}
    selected = str(persisted.get("selected_rail") or "")
    status = str(persisted.get("status") or "UNMAPPED")
    proof_reference = str(persisted.get("proof_reference") or "")
    candidates = MARKET_OWNER_RAIL_CANDIDATES.get(market.slug, ())
    rail = rails_by_slug.get(selected) if selected else None
    blockers = settlement_route_blockers(
        route_status=status,
        selected_rail=selected,
        proof_reference=proof_reference,
        market_payout_ready=bool(market.payout_ready),
        market_south_africa_verified=bool(market.south_africa_verified),
        rail=rail,
        candidate_rails=candidates,
    )
    priority = priority_for(market.slug)
    return {
        "market": market.slug,
        "market_display_name": market.display_name,
        "priority_rank": priority.rank,
        "priority_tier": priority.tier,
        "status": status,
        "selected_rail": selected,
        "candidate_rails": list(candidates),
        "proof_reference": proof_reference,
        "notes": str(persisted.get("notes") or ""),
        "verified_at": persisted.get("verified_at") or None,
        "ready": not blockers,
        "blockers": list(blockers),
        "market_payout_ready": bool(market.payout_ready),
        "market_south_africa_verified": bool(market.south_africa_verified),
        "owner_rail": rail,
        "human_withdrawal_required": bool(rail and rail.get("human_withdrawal_required")),
        "receipt_ready": bool(rail and rail.get("ready") and rail.get("payout_receive_enabled")),
        "final_withdrawal_managed_outside_amarktai": bool(rail and rail.get("human_withdrawal_required")),
    }


def settlement_routes_snapshot() -> dict[str, Any]:
    catalog = load_settlement_route_catalog()
    rails = payment_rail_snapshot()["rows"]
    rails_by_slug = {row["slug"]: row for row in rails}
    markets = list(Marketplace.objects.filter(slug__in=ACTIVE_MARKETS))
    markets.sort(key=lambda market: priority_for(market.slug).rank)
    rows = [settlement_route_row(market, catalog=catalog, rails_by_slug=rails_by_slug) for market in markets]
    return {
        "section": "settlement-routes",
        "rows": rows,
        "meta": {
            "catalog_version": SETTLEMENT_ROUTE_VERSION,
            "ready_routes": sum(1 for row in rows if row["ready"]),
            "blocked_routes": sum(1 for row in rows if not row["ready"]),
            "human_withdrawal_routes": sum(1 for row in rows if row["human_withdrawal_required"]),
            "truth": (
                "A marketplace route is ready when autonomous earning can deliver funds into a verified owner receipt rail. "
                "A later human withdrawal to a personal or business bank account is allowed and is intentionally outside AmarktAI."
            ),
        },
    }


@transaction.atomic
def update_market_settlement_route(
    market_slug: str,
    *,
    status: str,
    selected_rail: str = "",
    proof_reference: str = "",
    notes: str = "",
    actor: str = "owner",
) -> dict[str, Any]:
    market = Marketplace.objects.select_for_update().get(slug=market_slug)
    if market.slug not in ACTIVE_MARKETS:
        raise ValueError("market_not_active_earning_candidate")
    status = str(status or "").upper()
    if status not in ROUTE_STATES:
        raise ValueError("invalid_settlement_route_status")
    selected = _clean_text(selected_rail, limit=80)
    if selected and selected not in DEFAULT_PAYMENT_RAILS:
        raise ValueError("unknown_owner_payment_rail")
    candidates = MARKET_OWNER_RAIL_CANDIDATES.get(market.slug, ())
    if selected and candidates and selected not in candidates:
        raise ValueError("owner_payment_rail_not_in_market_candidates")

    setting, _ = SystemSetting.objects.select_for_update().get_or_create(
        key=SETTLEMENT_ROUTE_SETTING_KEY,
        defaults={"value": default_settlement_route_catalog(), "sensitive": False},
    )
    catalog = _merge_catalog(deepcopy(setting.value))
    record = {
        "status": status,
        "selected_rail": selected,
        "proof_reference": _clean_text(proof_reference, limit=255),
        "notes": _clean_text(notes, limit=1000),
        "verified_at": timezone.now().isoformat() if status == "VERIFIED" else None,
    }
    catalog["routes"][market.slug] = record

    rails_by_slug = {row["slug"]: row for row in payment_rail_snapshot()["rows"]}
    row = settlement_route_row(market, catalog=catalog, rails_by_slug=rails_by_slug)
    if status == "VERIFIED" and row["blockers"]:
        raise ValueError("settlement_route_not_ready:" + ",".join(row["blockers"]))

    setting.value = catalog
    setting.sensitive = False
    setting.save(update_fields=["value", "sensitive", "updated_at"])
    AuditEvent.objects.create(
        event_type="treasury.market_settlement_route_updated",
        actor=str(actor)[:120],
        metadata={
            "market": market.slug,
            "status": row["status"],
            "selected_rail": row["selected_rail"],
            "ready": row["ready"],
            "human_withdrawal_required": row["human_withdrawal_required"],
            "proof_reference_present": bool(row["proof_reference"]),
            "blockers": row["blockers"],
        },
    )
    return row
