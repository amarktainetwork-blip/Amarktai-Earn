from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, MarketServiceListing, Marketplace, ServiceOffering
from control.services.channel_commercial import commercial_pricing_row
from control.services.channel_packages import PACKAGE_SPECS
from control.services.seller_services import service_capability_blockers


PRIORITY_PACKAGE_SLUGS = frozenset(spec.slug for spec in PACKAGE_SPECS)
SELF_PUBLICATION_BLOCKERS = {
    "contra": {"FIRST_SERVICE_LISTING_NOT_PROVEN", "PUBLIC_AUTOMATION_CONTRACT_NOT_VERIFIED"},
    "rapidapi": {"API_LISTING_NOT_PUBLISHED", "PUBLISHING_AUTOMATION_NOT_VERIFIED"},
    "apify-store": {"ACTOR_NOT_PUBLISHED"},
    "lemon-squeezy": {"FIRST_PRODUCT_NOT_PUBLISHED"},
}
PUBLICATION_BLOCKER_BY_MARKET = {
    "contra": "FIRST_SERVICE_LISTING_NOT_PROVEN",
    "rapidapi": "API_LISTING_NOT_PUBLISHED",
    "apify-store": "ACTOR_NOT_PUBLISHED",
    "lemon-squeezy": "FIRST_PRODUCT_NOT_PUBLISHED",
}


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _listing(package_slug: str, *, for_update: bool = False) -> MarketServiceListing:
    if package_slug not in PRIORITY_PACKAGE_SLUGS:
        raise KeyError("unknown_priority_package")
    query = MarketServiceListing.objects.select_related("offering", "marketplace", "marketplace__integration_profile")
    if for_update:
        query = query.select_for_update()
    return query.get(offering__slug=package_slug)


def priority_manual_publication_blockers(listing: MarketServiceListing) -> list[str]:
    market = listing.marketplace
    offering = listing.offering
    blockers = list(service_capability_blockers(offering))

    if not market.enabled:
        blockers.append("MARKET_DISABLED")
    if market.status != Marketplace.Status.LIVE:
        blockers.append("MARKET_NOT_LIVE")
    if not market.payout_ready:
        blockers.append("PAYOUT_NOT_READY")
    if not market.south_africa_verified:
        blockers.append("SOUTH_AFRICA_PAYOUT_NOT_VERIFIED")

    try:
        profile = market.integration_profile
    except Exception:
        profile = None
    if profile is None:
        blockers.append("MARKET_SELLER_PROFILE_MISSING")
    else:
        if not profile.seller_capabilities.get("publish_service"):
            blockers.append("PUBLISH_SERVICE_CAPABILITY_NOT_VERIFIED")
        if not profile.policy_verified:
            blockers.append("MARKET_SERVICE_TERMS_NOT_VERIFIED")
        if profile.hosting_policy == "OFFHOST_SETTLEMENT_REQUIRED":
            blockers.append("OFFHOST_SETTLEMENT_REQUIRED")
        elif profile.hosting_policy != "WEBDOCK_SAFE":
            blockers.append("WEBDOCK_HOSTING_POLICY_UNVERIFIED")
        ignored = SELF_PUBLICATION_BLOCKERS.get(market.slug, set())
        blockers.extend(str(code) for code in (profile.blockers or []) if str(code) not in ignored)

    policy = market.policy_versions.order_by("-checked_at", "-created_at").first()
    if policy is None or not policy.automation_allowed:
        blockers.append("MARKET_SERVICE_TERMS_NOT_VERIFIED")
    elif not policy.webdock_compatible:
        blockers.append("MARKET_RUNTIME_NOT_COMPATIBLE")

    commercial = commercial_pricing_row(listing)
    if not commercial["prepared"]:
        blockers.extend(commercial["blockers"] or ["COMMERCIAL_PUBLIC_PRICE_NOT_READY"])
    if not commercial["owner_approved"]:
        blockers.append("OWNER_COMMERCIAL_PRICE_APPROVAL_REQUIRED")
    public_price = _decimal(commercial["public_price"])
    if public_price <= 0:
        blockers.append("COMMERCIAL_PUBLIC_PRICE_NOT_READY")
    if public_price < _decimal(offering.minimum_profitable_price):
        blockers.append("COMMERCIAL_PRICE_BELOW_PROFITABLE_FLOOR")

    if listing.status == MarketServiceListing.Status.PUBLISHED:
        blockers.append("SERVICE_LISTING_ALREADY_PUBLISHED")
    if listing.remote_listing_id or listing.remote_reference or listing.published_at:
        blockers.append("PARTIAL_REMOTE_PUBLICATION_STATE_REQUIRES_RECONCILIATION")

    return list(dict.fromkeys(blockers))


@transaction.atomic
def record_priority_manual_publication(
    package_slug: str,
    *,
    remote_listing_id: Any,
    remote_reference: Any,
    remote_version: Any = "",
    evidence_reference: Any,
    actor: str = "owner",
) -> dict:
    listing = _listing(package_slug, for_update=True)
    blockers = priority_manual_publication_blockers(listing)
    if blockers:
        raise ValueError("PRIORITY_PUBLICATION_BLOCKED:" + ",".join(blockers))

    remote_id = _clean(remote_listing_id, 255)
    remote_ref = _clean(remote_reference, 700)
    remote_ver = _clean(remote_version, 80)
    evidence_ref = _clean(evidence_reference, 700)
    if not remote_id or not remote_ref or not evidence_ref:
        raise ValueError("PRIORITY_PUBLICATION_EVIDENCE_REQUIRED")

    commercial = commercial_pricing_row(listing)
    price = _decimal(commercial["public_price"])
    now = timezone.now()
    metadata = dict(listing.platform_metadata or {})
    metadata["publication_evidence"] = {
        "mode": "OWNER_RECONCILED_REMOTE_PUBLICATION",
        "evidence_reference": evidence_ref,
        "recorded_at": now.isoformat(),
        "external_mutation_performed_by_amarktai": False,
        "commercial_pricing_source": commercial["source"],
    }
    listing.remote_listing_id = remote_id
    listing.remote_reference = remote_ref
    listing.remote_version = remote_ver
    listing.published_price = price
    listing.status = MarketServiceListing.Status.PUBLISHED
    listing.published_at = now
    listing.last_synced_at = now
    listing.failure_code = ""
    listing.failure_detail = ""
    policy = listing.marketplace.policy_versions.order_by("-checked_at", "-created_at").first()
    listing.policy_hash = policy.policy_hash if policy else ""
    listing.platform_metadata = metadata
    listing.save()

    profile = listing.marketplace.integration_profile
    publication_blocker = PUBLICATION_BLOCKER_BY_MARKET.get(listing.marketplace.slug)
    if publication_blocker and publication_blocker in (profile.blockers or []):
        profile.blockers = [code for code in profile.blockers if code != publication_blocker]
        profile.save(update_fields=["blockers", "updated_at"])

    AuditEvent.objects.create(
        event_type="channel.manual_publication_reconciled",
        actor=str(actor)[:120],
        metadata={
            "market": listing.marketplace.slug,
            "package_slug": package_slug,
            "listing_id": str(listing.id),
            "remote_listing_id": remote_id,
            "remote_reference": remote_ref,
            "published_price": str(price),
            "currency": listing.currency,
            "external_mutation_performed": False,
            "payout_truth_changed": False,
        },
    )
    return priority_publication_row(listing)


def priority_publication_row(listing: MarketServiceListing) -> dict:
    listing = MarketServiceListing.objects.select_related("offering", "marketplace").get(pk=listing.pk)
    published = bool(
        listing.status == MarketServiceListing.Status.PUBLISHED
        and listing.remote_listing_id
        and listing.remote_reference
        and listing.published_at
    )
    return {
        "market": listing.marketplace.slug,
        "package_slug": listing.offering.slug,
        "listing_status": listing.status,
        "published": published,
        "remote_listing_id": listing.remote_listing_id if published else "",
        "remote_reference": listing.remote_reference if published else "",
        "remote_version": listing.remote_version if published else "",
        "published_price": str(listing.published_price) if published else None,
        "currency": listing.currency,
        "published_at": listing.published_at.isoformat() if published else None,
        "record_blockers": [] if published else priority_manual_publication_blockers(listing),
        "external_mutation_allowed": False,
    }


def priority_publication_snapshot() -> dict:
    package_slugs = [spec.slug for spec in PACKAGE_SPECS]
    listings = {
        row.offering.slug: row
        for row in MarketServiceListing.objects.select_related("offering", "marketplace", "marketplace__integration_profile")
        .filter(offering__slug__in=package_slugs)
    }
    rows = [priority_publication_row(listings[spec.slug]) for spec in PACKAGE_SPECS if spec.slug in listings]
    return {
        "section": "priority-channel-publications",
        "rows": rows,
        "meta": {
            "total_packages": len(PACKAGE_SPECS),
            "published_packages": sum(1 for row in rows if row["published"]),
            "recordable_now": sum(1 for row in rows if not row["published"] and not row["record_blockers"]),
            "external_mutation_allowed": False,
            "banking_dependency_deferred": True,
            "truth": "This surface reconciles owner-performed remote publication evidence only. It never publishes externally and never changes payout readiness.",
        },
    }
