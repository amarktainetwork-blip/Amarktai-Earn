from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, SystemSetting


PAYMENT_RAIL_SETTING_KEY = "treasury.payment_rails.v1"
PAYMENT_RAIL_VERSION = 1

RAIL_STATES = (
    "NOT_CONFIGURED",
    "ACCOUNT_PROOF_REQUIRED",
    "PENDING_EXTERNAL_APPROVAL",
    "VERIFIED",
    "BLOCKED",
    "PAUSED",
)


DEFAULT_PAYMENT_RAILS: dict[str, dict[str, Any]] = {
    "paystack": {
        "display_name": "Paystack",
        "category": "PAYMENT_PROCESSOR",
        "candidate_capabilities": ["DIRECT_CHECKOUT", "PAYMENT_LINK", "SUBSCRIPTION", "BANK_SETTLEMENT"],
    },
    "paypal": {
        "display_name": "PayPal",
        "category": "PAYMENT_AND_PAYOUT_RAIL",
        "candidate_capabilities": ["DIRECT_CHECKOUT", "MARKETPLACE_PAYOUT_RECEIPT"],
    },
    "wise": {
        "display_name": "Wise",
        "category": "TREASURY_RAIL",
        "candidate_capabilities": ["PAYOUT_RECEIPT", "BANK_TRANSFER", "FX"],
    },
    "local-bank": {
        "display_name": "South African Bank",
        "category": "FINAL_TREASURY_DESTINATION",
        "candidate_capabilities": ["FINAL_SETTLEMENT"],
    },
    "payoneer": {
        "display_name": "Payoneer",
        "category": "PAYOUT_RAIL",
        "candidate_capabilities": ["MARKETPLACE_PAYOUT_RECEIPT"],
    },
    "yoco": {
        "display_name": "Yoco",
        "category": "PAYMENT_PROCESSOR",
        "candidate_capabilities": ["DIRECT_CHECKOUT", "PAYMENT_LINK", "BANK_SETTLEMENT"],
    },
}


def _default_record(slug: str) -> dict[str, Any]:
    definition = DEFAULT_PAYMENT_RAILS[slug]
    return {
        "slug": slug,
        "display_name": definition["display_name"],
        "category": definition["category"],
        "candidate_capabilities": list(definition["candidate_capabilities"]),
        "status": "NOT_CONFIGURED",
        "south_africa_verified": False,
        "checkout_enabled": False,
        "payout_receive_enabled": False,
        "final_settlement_enabled": False,
        "proof_reference": "",
        "verified_at": None,
        "owner_action": "Verify the owner account, South Africa eligibility, and the real settlement path before enabling this rail.",
        "notes": "",
    }


def default_payment_rail_catalog() -> dict[str, Any]:
    return {
        "version": PAYMENT_RAIL_VERSION,
        "rails": {slug: _default_record(slug) for slug in DEFAULT_PAYMENT_RAILS},
    }


def _clean_text(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _merge_catalog(raw: Any) -> dict[str, Any]:
    catalog = default_payment_rail_catalog()
    if not isinstance(raw, dict):
        return catalog
    persisted = raw.get("rails")
    if not isinstance(persisted, dict):
        return catalog

    for slug, record in catalog["rails"].items():
        candidate = persisted.get(slug)
        if not isinstance(candidate, dict):
            continue
        status = str(candidate.get("status") or record["status"]).strip().upper()
        if status not in RAIL_STATES:
            status = "BLOCKED"
        record.update({
            "status": status,
            "south_africa_verified": _coerce_bool(candidate.get("south_africa_verified")),
            "checkout_enabled": _coerce_bool(candidate.get("checkout_enabled")),
            "payout_receive_enabled": _coerce_bool(candidate.get("payout_receive_enabled")),
            "final_settlement_enabled": _coerce_bool(candidate.get("final_settlement_enabled")),
            "proof_reference": _clean_text(candidate.get("proof_reference"), limit=255),
            "verified_at": candidate.get("verified_at") or None,
            "owner_action": _clean_text(candidate.get("owner_action"), limit=500) or record["owner_action"],
            "notes": _clean_text(candidate.get("notes"), limit=1000),
        })
    return catalog


def load_payment_rail_catalog() -> dict[str, Any]:
    setting = SystemSetting.objects.filter(key=PAYMENT_RAIL_SETTING_KEY).first()
    return _merge_catalog(setting.value if setting else None)


def _record_ready(record: dict[str, Any]) -> bool:
    has_live_capability = bool(
        record.get("checkout_enabled")
        or record.get("payout_receive_enabled")
        or record.get("final_settlement_enabled")
    )
    return bool(
        record.get("status") == "VERIFIED"
        and record.get("south_africa_verified") is True
        and record.get("proof_reference")
        and has_live_capability
    )


def public_payment_rail_row(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": record["slug"],
        "display_name": record["display_name"],
        "category": record["category"],
        "candidate_capabilities": list(record["candidate_capabilities"]),
        "status": record["status"],
        "south_africa_verified": bool(record["south_africa_verified"]),
        "checkout_enabled": bool(record["checkout_enabled"]),
        "payout_receive_enabled": bool(record["payout_receive_enabled"]),
        "final_settlement_enabled": bool(record["final_settlement_enabled"]),
        "ready": _record_ready(record),
        "proof_reference": record["proof_reference"],
        "verified_at": record["verified_at"],
        "owner_action": record["owner_action"],
        "notes": record["notes"],
    }


def payment_rail_snapshot() -> dict[str, Any]:
    catalog = load_payment_rail_catalog()
    rows = [public_payment_rail_row(catalog["rails"][slug]) for slug in DEFAULT_PAYMENT_RAILS]
    return {
        "section": "banking",
        "rows": rows,
        "meta": {
            "catalog_version": PAYMENT_RAIL_VERSION,
            "ready_rails": sum(1 for row in rows if row["ready"]),
            "action_required": sum(1 for row in rows if not row["ready"]),
            "checkout_ready": sum(1 for row in rows if row["ready"] and row["checkout_enabled"]),
            "payout_receive_ready": sum(1 for row in rows if row["ready"] and row["payout_receive_enabled"]),
            "final_settlement_ready": sum(1 for row in rows if row["ready"] and row["final_settlement_enabled"]),
            "truth": "Candidate capability labels are planning metadata only. A payment rail becomes ready only after owner-account proof, South Africa verification, an explicit live capability, and a non-secret proof reference are all recorded.",
        },
    }


def _serialize_verified_at(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


@transaction.atomic
def update_payment_rail_proof(
    slug: str,
    *,
    status: str,
    south_africa_verified: Any = False,
    checkout_enabled: Any = False,
    payout_receive_enabled: Any = False,
    final_settlement_enabled: Any = False,
    proof_reference: Any = "",
    owner_action: Any = "",
    notes: Any = "",
    actor: str = "owner",
) -> dict[str, Any]:
    if slug not in DEFAULT_PAYMENT_RAILS:
        raise KeyError("unknown_payment_rail")

    status = str(status or "").strip().upper()
    if status not in RAIL_STATES:
        raise ValueError("invalid_payment_rail_status")

    sa_verified = _coerce_bool(south_africa_verified)
    checkout = _coerce_bool(checkout_enabled)
    payout_receive = _coerce_bool(payout_receive_enabled)
    final_settlement = _coerce_bool(final_settlement_enabled)
    proof = _clean_text(proof_reference, limit=255)
    action = _clean_text(owner_action, limit=500)
    note = _clean_text(notes, limit=1000)

    if status == "VERIFIED":
        if not sa_verified:
            raise ValueError("south_africa_proof_required")
        if not proof:
            raise ValueError("proof_reference_required")
        if not (checkout or payout_receive or final_settlement):
            raise ValueError("live_payment_capability_required")

    setting, _ = SystemSetting.objects.select_for_update().get_or_create(
        key=PAYMENT_RAIL_SETTING_KEY,
        defaults={"value": default_payment_rail_catalog(), "sensitive": False},
    )
    catalog = _merge_catalog(deepcopy(setting.value))
    record = catalog["rails"][slug]
    now = timezone.now()
    record.update({
        "status": status,
        "south_africa_verified": sa_verified,
        "checkout_enabled": checkout,
        "payout_receive_enabled": payout_receive,
        "final_settlement_enabled": final_settlement,
        "proof_reference": proof,
        "verified_at": _serialize_verified_at(now) if status == "VERIFIED" else None,
        "owner_action": action or _default_record(slug)["owner_action"],
        "notes": note,
    })

    setting.value = catalog
    setting.sensitive = False
    setting.save(update_fields=["value", "sensitive", "updated_at"])

    public = public_payment_rail_row(record)
    AuditEvent.objects.create(
        event_type="treasury.payment_rail_proof_updated",
        actor=str(actor)[:120],
        metadata={
            "rail": slug,
            "status": public["status"],
            "south_africa_verified": public["south_africa_verified"],
            "checkout_enabled": public["checkout_enabled"],
            "payout_receive_enabled": public["payout_receive_enabled"],
            "final_settlement_enabled": public["final_settlement_enabled"],
            "ready": public["ready"],
            "proof_reference_present": bool(public["proof_reference"]),
        },
    )
    return public
