from __future__ import annotations

from copy import deepcopy
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, MarketIntegrationProfile, Marketplace


ONBOARDING_VERSION = 1
PRIORITY_MARKETS = ("contra", "rapidapi", "apify-store", "lemon-squeezy")

_DEFINITIONS = {
    "contra": {
        "checks": ("account_configured", "identity_verified", "manual_storefront_contract_verified", "service_terms_verified"),
        "policy_check": "service_terms_verified",
        "capabilities": {
            "publish_service": "manual_storefront_contract_verified",
            "receive_orders": "manual_storefront_contract_verified",
            "order_status": "manual_storefront_contract_verified",
            "service_delivery": "manual_storefront_contract_verified",
            "project_sales": "manual_storefront_contract_verified",
            "subscription_sales": "manual_storefront_contract_verified",
        },
        "cleared_blockers": {
            "account_configured": ("ACCOUNT_NOT_CONFIGURED",),
            "identity_verified": ("IDENTITY_NOT_VERIFIED",),
        },
    },
    "rapidapi": {
        "checks": ("provider_account_configured", "provider_contract_verified", "service_terms_verified"),
        "policy_check": "service_terms_verified",
        "capabilities": {
            "publish_service": "provider_contract_verified",
            "receive_orders": "provider_contract_verified",
            "order_status": "provider_contract_verified",
            "service_delivery": "provider_contract_verified",
            "usage_metering": "provider_contract_verified",
            "pay_per_call": "provider_contract_verified",
            "subscription_sales": "provider_contract_verified",
        },
        "cleared_blockers": {
            "provider_account_configured": ("PROVIDER_ACCOUNT_NOT_CONFIGURED",),
        },
    },
    "apify-store": {
        "checks": ("creator_account_configured", "creator_identity_verified", "actor_publication_contract_verified", "store_terms_verified"),
        "policy_check": "store_terms_verified",
        "capabilities": {
            "publish_service": "actor_publication_contract_verified",
            "usage_metering": "actor_publication_contract_verified",
            "pay_per_call": "actor_publication_contract_verified",
        },
        "cleared_blockers": {
            "creator_account_configured": ("CREATOR_ACCOUNT_NOT_CONFIGURED",),
            "creator_identity_verified": ("KYC_NOT_VERIFIED",),
        },
    },
    "lemon-squeezy": {
        "checks": ("merchant_account_configured", "merchant_identity_verified", "api_webhook_contract_verified", "service_terms_verified"),
        "policy_check": "service_terms_verified",
        "capabilities": {
            "publish_service": "api_webhook_contract_verified",
            "receive_orders": "api_webhook_contract_verified",
            "order_status": "api_webhook_contract_verified",
            "seller_webhooks": "api_webhook_contract_verified",
            "subscription_sales": "api_webhook_contract_verified",
            "pay_per_call": "api_webhook_contract_verified",
        },
        "cleared_blockers": {
            "merchant_account_configured": ("MERCHANT_ACCOUNT_NOT_CONFIGURED",),
            "merchant_identity_verified": ("KYC_NOT_VERIFIED",),
            "api_webhook_contract_verified": ("API_WEBHOOK_NOT_WIRED",),
        },
    },
}


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _operator_record(profile: MarketIntegrationProfile) -> dict:
    evidence = profile.evidence if isinstance(profile.evidence, dict) else {}
    raw = evidence.get("operator_onboarding")
    return deepcopy(raw) if isinstance(raw, dict) else {}


def _normalized_record(market_slug: str, raw: dict | None = None) -> dict:
    definition = _DEFINITIONS[market_slug]
    raw = raw if isinstance(raw, dict) else {}
    checks = {key: _bool((raw.get("checks") or {}).get(key)) for key in definition["checks"]}
    return {
        "version": ONBOARDING_VERSION,
        "market": market_slug,
        "checks": checks,
        "proof_reference": _clean(raw.get("proof_reference"), 500),
        "notes": _clean(raw.get("notes"), 1500),
        "verified_at": raw.get("verified_at") or None,
    }


def _apply_effective_profile(profile: MarketIntegrationProfile, record: dict) -> bool:
    definition = _DEFINITIONS[profile.marketplace.slug]
    checks = record["checks"]
    seller_capabilities = dict(profile.seller_capabilities or {})
    for capability, check in definition["capabilities"].items():
        seller_capabilities[capability] = bool(checks.get(check))

    blockers = list(profile.blockers or [])
    cleared = set()
    for check, codes in definition["cleared_blockers"].items():
        if checks.get(check):
            cleared.update(codes)
    blockers = [code for code in blockers if code not in cleared]

    policy_verified = bool(checks.get(definition["policy_check"]))
    all_checks = all(checks.values())
    changed = False
    if profile.seller_capabilities != seller_capabilities:
        profile.seller_capabilities = seller_capabilities
        changed = True
    if profile.blockers != blockers:
        profile.blockers = blockers
        changed = True
    if profile.policy_verified != policy_verified:
        profile.policy_verified = policy_verified
        changed = True
    desired_status = "MANUAL_READY_PAYOUT_BLOCKED" if all_checks else "BLOCKED"
    if profile.automation_status != desired_status:
        profile.automation_status = desired_status
        changed = True
    if changed:
        profile.save(update_fields=["seller_capabilities", "blockers", "policy_verified", "automation_status", "updated_at"])

    policy = profile.marketplace.policy_versions.order_by("-checked_at", "-created_at").first()
    if policy and (
        policy.automation_allowed != policy_verified
        or policy.webdock_compatible != (profile.hosting_policy == "WEBDOCK_SAFE")
    ):
        policy.automation_allowed = policy_verified
        policy.webdock_compatible = profile.hosting_policy == "WEBDOCK_SAFE"
        policy.checked_at = timezone.now()
        policy.snapshot = {
            **(policy.snapshot or {}),
            "operator_terms_proof": {
                "verified": policy_verified,
                "proof_reference_present": bool(record["proof_reference"]),
                "onboarding_version": ONBOARDING_VERSION,
            },
        }
        policy.save(update_fields=["automation_allowed", "webdock_compatible", "checked_at", "snapshot", "updated_at"])
        changed = True
    return changed


@transaction.atomic
def reapply_priority_channel_onboarding() -> dict[str, int]:
    applied = unchanged = 0
    for profile in MarketIntegrationProfile.objects.select_related("marketplace").filter(marketplace__slug__in=PRIORITY_MARKETS):
        raw = _operator_record(profile)
        record = _normalized_record(profile.marketplace.slug, raw)
        if _apply_effective_profile(profile, record):
            applied += 1
        else:
            unchanged += 1
    return {"applied": applied, "unchanged": unchanged, "total": applied + unchanged}


@transaction.atomic
def update_priority_channel_onboarding(
    market_slug: str,
    *,
    checks: dict,
    proof_reference: Any,
    notes: Any = "",
    actor: str = "owner",
) -> dict:
    if market_slug not in _DEFINITIONS:
        raise KeyError("unknown_priority_channel")
    profile = MarketIntegrationProfile.objects.select_related("marketplace").select_for_update().get(marketplace__slug=market_slug)
    definition = _DEFINITIONS[market_slug]
    supplied = checks if isinstance(checks, dict) else {}
    unknown = set(supplied) - set(definition["checks"])
    if unknown:
        raise ValueError("UNKNOWN_ONBOARDING_CHECK:" + ",".join(sorted(unknown)))
    record = _normalized_record(market_slug, {
        "checks": {key: supplied.get(key, False) for key in definition["checks"]},
        "proof_reference": proof_reference,
        "notes": notes,
        "verified_at": timezone.now().isoformat(),
    })
    if any(record["checks"].values()) and not record["proof_reference"]:
        raise ValueError("ONBOARDING_PROOF_REFERENCE_REQUIRED")

    evidence = dict(profile.evidence or {})
    evidence["operator_onboarding"] = record
    profile.evidence = evidence
    profile.save(update_fields=["evidence", "updated_at"])
    _apply_effective_profile(profile, record)
    AuditEvent.objects.create(
        event_type="channel.onboarding_evidence_updated",
        actor=str(actor)[:120],
        metadata={
            "market": market_slug,
            "checks": record["checks"],
            "proof_reference_present": bool(record["proof_reference"]),
            "payout_truth_changed": False,
        },
    )
    return priority_channel_onboarding_row(market_slug)


def priority_channel_onboarding_row(market_slug: str) -> dict:
    if market_slug not in _DEFINITIONS:
        raise KeyError("unknown_priority_channel")
    market = Marketplace.objects.select_related("integration_profile").get(slug=market_slug)
    profile = market.integration_profile
    record = _normalized_record(market_slug, _operator_record(profile))
    checks = record["checks"]
    return {
        "market": market_slug,
        "display_name": market.display_name,
        "checks": checks,
        "required_checks": list(_DEFINITIONS[market_slug]["checks"]),
        "account_contract_ready": all(checks.values()),
        "policy_verified": bool(profile.policy_verified),
        "proof_reference": record["proof_reference"],
        "notes": record["notes"],
        "verified_at": record["verified_at"],
        "effective_seller_capabilities": dict(profile.seller_capabilities or {}),
        "profile_blockers": list(profile.blockers or []),
        "payout_ready": bool(market.payout_ready),
        "south_africa_verified": bool(market.south_africa_verified),
        "banking_dependency_deferred": True,
    }


def priority_channel_onboarding_snapshot() -> dict:
    rows = [priority_channel_onboarding_row(slug) for slug in PRIORITY_MARKETS if Marketplace.objects.filter(slug=slug).exists()]
    return {
        "section": "priority-channel-onboarding",
        "rows": rows,
        "meta": {
            "channels": len(rows),
            "account_contract_ready": sum(1 for row in rows if row["account_contract_ready"]),
            "policy_verified": sum(1 for row in rows if row["policy_verified"]),
            "payout_ready": sum(1 for row in rows if row["payout_ready"] and row["south_africa_verified"]),
            "banking_dependency_deferred": True,
            "truth": "Account, identity, seller-contract and service-terms proof is separate from payout readiness. This control never creates a payout account, enables Banking, or marks South Africa settlement ready.",
        },
    }
