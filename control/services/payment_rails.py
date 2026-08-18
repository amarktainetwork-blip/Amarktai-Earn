from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, SystemSetting


PAYMENT_RAIL_SETTING_KEY = "treasury.payment_rails.v1"
PAYMENT_RAIL_VERSION = 4

RAIL_STATES = (
    "NOT_CONFIGURED",
    "ACCOUNT_PROOF_REQUIRED",
    "PENDING_EXTERNAL_APPROVAL",
    "VERIFIED",
    "BLOCKED",
    "PAUSED",
)

# Earn stores proof/status only. Bank-account numbers and withdrawal credentials
# remain in the external provider account.
DEFAULT_PAYMENT_RAILS: dict[str, dict[str, Any]] = {
    "paystack": {
        "display_name": "Paystack",
        "category": "OWNED_REVENUE_PROCESSOR",
        "candidate_capabilities": ["DIRECT_CHECKOUT", "PAYMENT_LINK", "SUBSCRIPTION", "AUTOMATIC_EXTERNAL_PAYOUT"],
        "receipt_mode": "AUTOMATIC",
        "withdrawal_mode": "PROVIDER_MANAGED",
        "human_withdrawal_required": False,
        "external_configuration_note": "Configure the real payout bank account inside Paystack only. AmarktAI stores no bank details.",
    },
    "paypal": {
        "display_name": "PayPal",
        "category": "MARKETPLACE_PAYOUT_RECEIPT",
        "candidate_capabilities": ["MARKETPLACE_PAYOUT_RECEIPT", "DIRECT_CHECKOUT"],
        "receipt_mode": "AUTOMATIC",
        "withdrawal_mode": "HUMAN_WITHDRAWAL",
        "human_withdrawal_required": True,
        "external_configuration_note": "Marketplace payouts may arrive automatically. A human handles any later withdrawal outside AmarktAI.",
    },
    "provider-bank": {
        "display_name": "Provider-managed bank payout",
        "category": "FIAT_MARKETPLACE_PAYOUT",
        "candidate_capabilities": ["USD_BANK_TRANSFER", "MARKETPLACE_PAYOUT_RECEIPT"],
        "receipt_mode": "AUTOMATIC_AFTER_PROVIDER_ONBOARDING",
        "withdrawal_mode": "PROVIDER_MANAGED",
        "human_withdrawal_required": False,
        "external_configuration_note": "Complete bank onboarding with the marketplace or its processor. AmarktAI stores proof, never bank details.",
    },
    "stripe-connect": {
        "display_name": "Stripe Connect payout",
        "category": "FIAT_MARKETPLACE_PAYOUT",
        "candidate_capabilities": ["MARKETPLACE_PAYOUT_RECEIPT"],
        "receipt_mode": "AUTOMATIC_AFTER_PROVIDER_ONBOARDING",
        "withdrawal_mode": "PROVIDER_MANAGED",
        "human_withdrawal_required": False,
        "external_configuration_note": "Complete Stripe Connect onboarding externally. AmarktAI stores only status and receipt evidence.",
    },
    "wise": {
        "display_name": "Wise",
        "category": "SECONDARY_PAYOUT_RAIL",
        "candidate_capabilities": ["MARKETPLACE_PAYOUT_RECEIPT", "FX"],
        "receipt_mode": "AUTOMATIC_WHEN_SUPPORTED",
        "withdrawal_mode": "HUMAN_WITHDRAWAL",
        "human_withdrawal_required": True,
        "external_configuration_note": "Optional secondary payout rail. Account and destination details remain in Wise.",
    },
    "payoneer": {
        "display_name": "Payoneer",
        "category": "SECONDARY_PAYOUT_RAIL",
        "candidate_capabilities": ["MARKETPLACE_PAYOUT_RECEIPT"],
        "receipt_mode": "AUTOMATIC_WHEN_SUPPORTED",
        "withdrawal_mode": "HUMAN_WITHDRAWAL",
        "human_withdrawal_required": True,
        "external_configuration_note": "Optional secondary payout rail. Account and destination details remain in Payoneer.",
    },
}

# Accounts are deliberately grouped by whether opening them can unlock a real
# owner-usable receipt path. Stripe-only and credit-only channels are excluded.
# Opening an account never marks it ready; KYC/API/payout proof remains fail-closed.
ACCOUNT_SETUP_PLAN = {
    "open_now": (
        ("paystack", "Paystack", "Owned checkout, payment links and provider-managed South African settlement"),
        ("paypal", "PayPal", "Shared receipt rail for marketplaces and direct creator sales"),
        ("lemon-squeezy", "Lemon Squeezy", "Recurring products, subscriptions and direct digital commerce"),
        ("payhip", "Payhip", "One-off digital products and services paid directly into Paystack"),
        ("ko-fi", "Ko-fi", "Instant PayPal tips, shop sales, memberships and service commissions"),
        ("gumroad", "Gumroad", "Digital-product discovery and South African ZAR payout channel"),
        ("patreon", "Patreon", "Recurring memberships and one-time digital product revenue"),
        ("rapidapi", "RapidAPI Provider", "Recurring API subscriptions and usage revenue paid to PayPal"),
        ("apify-store", "Apify", "Paid Actors and data/automation products with execution kept on Apify"),
        ("taskbounty", "TaskBounty Solver", "API-native coding bounties with USD bank-transfer payout"),
        ("contra", "Contra", "Projects, services and payment links with non-Stripe payout options"),
        ("freelancer", "Freelancer.com", "Official API project discovery/bidding with PayPal or Payoneer withdrawal"),
        ("dealwork", "Dealwork", "Escrow-backed autonomous agent work; open account now to complete KYA and prove withdrawal"),
        ("algora", "Algora", "Coding bounty and contract income; open now to prove the owner payout method"),
        ("impact", "impact.com Partner", "Affiliate and referral commissions with automatic payout scheduling"),
        ("partnerstack", "PartnerStack", "B2B SaaS affiliate and referral commissions payable to PayPal"),
    ),
    "open_next": (),
    "optional": (
        ("wise", "Wise", "Secondary payout rail where a marketplace supports it"),
        ("payoneer", "Payoneer", "Secondary payout rail for Contra, Freelancer.com and other supported channels"),
    ),
}


def account_setup_snapshot() -> dict[str, list[dict[str, str]]]:
    return {
        group: [
            {"slug": slug, "display_name": name, "purpose": purpose}
            for slug, name, purpose in rows
        ]
        for group, rows in ACCOUNT_SETUP_PLAN.items()
    }


def _default_record(slug: str) -> dict[str, Any]:
    definition = DEFAULT_PAYMENT_RAILS[slug]
    return {
        "slug": slug,
        "display_name": definition["display_name"],
        "category": definition["category"],
        "candidate_capabilities": list(definition["candidate_capabilities"]),
        "receipt_mode": definition["receipt_mode"],
        "withdrawal_mode": definition["withdrawal_mode"],
        "human_withdrawal_required": bool(definition["human_withdrawal_required"]),
        "external_configuration_note": definition["external_configuration_note"],
        "status": "NOT_CONFIGURED",
        "south_africa_verified": False,
        "checkout_enabled": False,
        "payout_receive_enabled": False,
        "final_settlement_enabled": False,
        "proof_reference": "",
        "verified_at": None,
        "owner_action": "Verify the external account, South Africa eligibility, and its real receipt path before enabling this rail.",
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
        "receipt_mode": record["receipt_mode"],
        "withdrawal_mode": record["withdrawal_mode"],
        "human_withdrawal_required": bool(record["human_withdrawal_required"]),
        "external_configuration_note": record["external_configuration_note"],
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
        "stores_bank_details": False,
        "stores_private_keys": False,
    }


def payment_rail_snapshot() -> dict[str, Any]:
    catalog = load_payment_rail_catalog()
    rows = [public_payment_rail_row(catalog["rails"][slug]) for slug in DEFAULT_PAYMENT_RAILS]
    accounts = account_setup_snapshot()
    return {
        "section": "treasury",
        "rows": rows,
        "account_setup": accounts,
        "meta": {
            "catalog_version": PAYMENT_RAIL_VERSION,
            "ready_rails": sum(1 for row in rows if row["ready"]),
            "action_required": sum(1 for row in rows if not row["ready"]),
            "checkout_ready": sum(1 for row in rows if row["ready"] and row["checkout_enabled"]),
            "payout_receive_ready": sum(1 for row in rows if row["ready"] and row["payout_receive_enabled"]),
            "human_withdrawal_rails": sum(1 for row in rows if row["human_withdrawal_required"]),
            "accounts_open_now": len(accounts["open_now"]),
            "accounts_open_next": len(accounts["open_next"]),
            "optional_accounts": len(accounts["optional"]),
            "truth": (
                "AmarktAI tracks payment receipt and non-secret settlement evidence only. "
                "South African bank details, FNB withdrawal details, wallet private keys and exchange withdrawal secrets stay outside the dashboard. "
                "Human withdrawals are allowed and do not block autonomous earning or automatic marketplace payout receipt."
            ),
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
            "human_withdrawal_required": public["human_withdrawal_required"],
            "proof_reference_present": bool(public["proof_reference"]),
        },
    )
    return public
