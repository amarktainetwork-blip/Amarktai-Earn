from __future__ import annotations

from typing import Mapping


ROUTE_STATES = (
    "UNMAPPED",
    "PROPOSED",
    "VERIFIED",
    "BLOCKED",
    "PAUSED",
)


def settlement_route_blockers(
    *,
    route_status: str,
    selected_rail: str,
    proof_reference: str,
    market_payout_ready: bool,
    market_south_africa_verified: bool,
    rail: Mapping | None,
    candidate_rails: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return fail-closed blockers for a marketplace -> owner payment-rail route."""
    blockers: list[str] = []
    status = str(route_status or "UNMAPPED").upper()
    selected = str(selected_rail or "").strip()
    proof = str(proof_reference or "").strip()

    if status != "VERIFIED":
        blockers.append("SETTLEMENT_ROUTE_NOT_VERIFIED")
    if not selected:
        blockers.append("OWNER_PAYMENT_RAIL_NOT_SELECTED")
    if selected and candidate_rails and selected not in candidate_rails:
        blockers.append("OWNER_PAYMENT_RAIL_NOT_IN_MARKET_CANDIDATES")
    if not proof:
        blockers.append("SETTLEMENT_ROUTE_PROOF_REQUIRED")
    if not market_payout_ready:
        blockers.append("MARKET_PAYOUT_NOT_READY")
    if not market_south_africa_verified:
        blockers.append("MARKET_SOUTH_AFRICA_PAYOUT_NOT_VERIFIED")
    if selected:
        if not isinstance(rail, Mapping):
            blockers.append("OWNER_PAYMENT_RAIL_UNKNOWN")
        else:
            if rail.get("ready") is not True:
                blockers.append("OWNER_PAYMENT_RAIL_NOT_READY")
            if rail.get("south_africa_verified") is not True:
                blockers.append("OWNER_PAYMENT_RAIL_SOUTH_AFRICA_NOT_VERIFIED")
            if not (
                rail.get("payout_receive_enabled") is True
                or rail.get("final_settlement_enabled") is True
            ):
                blockers.append("OWNER_PAYMENT_RAIL_CANNOT_RECEIVE_SETTLEMENT")
    return tuple(dict.fromkeys(blockers))


def settlement_route_ready(**kwargs) -> bool:
    return not settlement_route_blockers(**kwargs)
