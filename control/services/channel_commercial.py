from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Any

from django.db import transaction

from control.models import AuditEvent, MarketServiceListing, ServiceOffering
from control.services.channel_packages import PACKAGE_SPECS


COMMERCIAL_PRICING_VERSION = 1
CENT = Decimal("0.01")
ZERO = Decimal("0")

_DEFAULT_MULTIPLIERS = {
    ServiceOffering.PricingModel.FIXED_PROJECT: Decimal("4.00"),
    ServiceOffering.PricingModel.PER_CALL: Decimal("3.00"),
    ServiceOffering.PricingModel.PER_UNIT: Decimal("3.00"),
    ServiceOffering.PricingModel.SUBSCRIPTION: Decimal("6.00"),
    ServiceOffering.PricingModel.OUTCOME: Decimal("4.00"),
}

_DEFAULT_PUBLIC_MINIMUMS = {
    ServiceOffering.PricingModel.FIXED_PROJECT: Decimal("49.00"),
    ServiceOffering.PricingModel.PER_CALL: Decimal("0.50"),
    ServiceOffering.PricingModel.PER_UNIT: Decimal("0.10"),
    ServiceOffering.PricingModel.SUBSCRIPTION: Decimal("29.00"),
    ServiceOffering.PricingModel.OUTCOME: Decimal("49.00"),
}

_ENV_MULTIPLIERS = {
    ServiceOffering.PricingModel.FIXED_PROJECT: "COMMERCIAL_FIXED_PROJECT_MULTIPLIER",
    ServiceOffering.PricingModel.PER_CALL: "COMMERCIAL_PER_CALL_MULTIPLIER",
    ServiceOffering.PricingModel.PER_UNIT: "COMMERCIAL_PER_UNIT_MULTIPLIER",
    ServiceOffering.PricingModel.SUBSCRIPTION: "COMMERCIAL_SUBSCRIPTION_MULTIPLIER",
    ServiceOffering.PricingModel.OUTCOME: "COMMERCIAL_OUTCOME_MULTIPLIER",
}

_ENV_MINIMUMS = {
    ServiceOffering.PricingModel.FIXED_PROJECT: "COMMERCIAL_FIXED_PROJECT_MIN_USD",
    ServiceOffering.PricingModel.PER_CALL: "COMMERCIAL_PER_CALL_MIN_USD",
    ServiceOffering.PricingModel.PER_UNIT: "COMMERCIAL_PER_UNIT_MIN_USD",
    ServiceOffering.PricingModel.SUBSCRIPTION: "COMMERCIAL_SUBSCRIPTION_MIN_USD",
    ServiceOffering.PricingModel.OUTCOME: "COMMERCIAL_OUTCOME_MIN_USD",
}


def _decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _money(value: Decimal) -> Decimal:
    return max(ZERO, value).quantize(CENT, rounding=ROUND_HALF_UP)


def _positive_env(name: str, default: Decimal) -> Decimal:
    value = _decimal(os.getenv(name, str(default)), default)
    return value if value > 0 else default


def _pricing_unit(pricing_model: str) -> str:
    return {
        ServiceOffering.PricingModel.FIXED_PROJECT: "PROJECT",
        ServiceOffering.PricingModel.PER_CALL: "CALL",
        ServiceOffering.PricingModel.PER_UNIT: "UNIT",
        ServiceOffering.PricingModel.SUBSCRIPTION: "MONTH",
        ServiceOffering.PricingModel.OUTCOME: "OUTCOME",
    }.get(pricing_model, "UNIT")


def _channel_manifest(listing: MarketServiceListing) -> dict:
    metadata = listing.platform_metadata if isinstance(listing.platform_metadata, dict) else {}
    manifest = metadata.get("channel_package")
    return manifest if isinstance(manifest, dict) else {}


def commercial_price_proposal(listing: MarketServiceListing) -> dict:
    offering = listing.offering
    manifest = _channel_manifest(listing)
    package_blockers = [str(item) for item in (manifest.get("pricing_blockers") or [])]
    blockers = list(package_blockers)

    floor = _money(max(_decimal(offering.minimum_profitable_price), ZERO))
    if floor <= 0:
        blockers.append("INTERNAL_ECONOMIC_FLOOR_NOT_PROVEN")

    if blockers:
        return {
            "version": COMMERCIAL_PRICING_VERSION,
            "state": "BLOCKED",
            "source": "AUTO_PROPOSAL",
            "currency": offering.currency,
            "pricing_model": offering.pricing_model,
            "billing_unit": _pricing_unit(offering.pricing_model),
            "minimum_profitable_price": str(floor),
            "public_price": None,
            "multiplier": None,
            "commercial_minimum": None,
            "blockers": list(dict.fromkeys(blockers)),
        }

    multiplier_default = _DEFAULT_MULTIPLIERS.get(offering.pricing_model, Decimal("4.00"))
    minimum_default = _DEFAULT_PUBLIC_MINIMUMS.get(offering.pricing_model, Decimal("1.00"))
    multiplier = _positive_env(_ENV_MULTIPLIERS.get(offering.pricing_model, "COMMERCIAL_DEFAULT_MULTIPLIER"), multiplier_default)
    commercial_minimum = _positive_env(_ENV_MINIMUMS.get(offering.pricing_model, "COMMERCIAL_DEFAULT_MIN_USD"), minimum_default)
    proposed = _money(max(floor * multiplier, commercial_minimum))

    return {
        "version": COMMERCIAL_PRICING_VERSION,
        "state": "AUTO_PROPOSED",
        "source": "AUTO_PROPOSAL",
        "currency": offering.currency,
        "pricing_model": offering.pricing_model,
        "billing_unit": _pricing_unit(offering.pricing_model),
        "minimum_profitable_price": str(floor),
        "public_price": str(proposed),
        "multiplier": str(multiplier),
        "commercial_minimum": str(commercial_minimum),
        "blockers": [],
    }


def _listing_for_package(package_slug: str, *, for_update: bool = False) -> MarketServiceListing:
    queryset = MarketServiceListing.objects.select_related("offering", "marketplace")
    if for_update:
        queryset = queryset.select_for_update()
    return queryset.get(offering__slug=package_slug)


def _safe_to_refresh(listing: MarketServiceListing) -> bool:
    return bool(
        listing.status != MarketServiceListing.Status.PUBLISHED
        and not listing.remote_listing_id
        and not listing.remote_reference
        and listing.published_at is None
    )


@transaction.atomic
def bootstrap_channel_commercial_pricing() -> dict[str, int]:
    created = updated = preserved = blocked = missing = 0
    for spec in PACKAGE_SPECS:
        try:
            listing = _listing_for_package(spec.slug, for_update=True)
        except MarketServiceListing.DoesNotExist:
            missing += 1
            continue

        metadata = dict(listing.platform_metadata or {})
        existing = metadata.get("commercial_pricing") if isinstance(metadata.get("commercial_pricing"), dict) else None
        if existing and existing.get("source") == "OWNER_OVERRIDE":
            preserved += 1
            blocked += int(bool(existing.get("blockers")))
            continue
        if not _safe_to_refresh(listing):
            preserved += 1
            continue

        proposal = commercial_price_proposal(listing)
        blocked += int(bool(proposal.get("blockers")))
        if existing is None:
            created += 1
        elif existing != proposal:
            updated += 1
        else:
            preserved += 1
        metadata["commercial_pricing"] = proposal
        listing.platform_metadata = metadata
        listing.save(update_fields=["platform_metadata", "updated_at"])

    return {
        "created": created,
        "updated": updated,
        "preserved": preserved,
        "blocked": blocked,
        "missing": missing,
        "total": len(PACKAGE_SPECS),
    }


@transaction.atomic
def set_owner_commercial_price(package_slug: str, *, price: Any, actor: str = "owner") -> dict:
    listing = _listing_for_package(package_slug, for_update=True)
    if not _safe_to_refresh(listing):
        raise ValueError("COMMERCIAL_PRICE_LOCKED_AFTER_PUBLICATION")

    proposal = commercial_price_proposal(listing)
    if proposal["blockers"]:
        raise ValueError("COMMERCIAL_PRICE_NOT_READY:" + ",".join(proposal["blockers"]))

    amount = _money(_decimal(price))
    floor = _money(_decimal(proposal["minimum_profitable_price"]))
    commercial_minimum = _money(_decimal(proposal.get("commercial_minimum")))
    launch_minimum = max(floor, commercial_minimum)
    if amount <= 0:
        raise ValueError("COMMERCIAL_PRICE_MUST_BE_POSITIVE")
    if amount < floor:
        raise ValueError("COMMERCIAL_PRICE_BELOW_PROFITABLE_FLOOR")
    if amount < launch_minimum:
        raise ValueError("COMMERCIAL_PRICE_BELOW_LAUNCH_MINIMUM")

    record = {
        **proposal,
        "state": "OWNER_APPROVED_DRAFT",
        "source": "OWNER_OVERRIDE",
        "public_price": str(amount),
        "blockers": [],
    }
    metadata = dict(listing.platform_metadata or {})
    metadata["commercial_pricing"] = record
    listing.platform_metadata = metadata
    listing.save(update_fields=["platform_metadata", "updated_at"])
    AuditEvent.objects.create(
        event_type="channel.commercial_price_updated",
        actor=str(actor)[:120],
        metadata={
            "package_slug": package_slug,
            "market": listing.marketplace.slug,
            "price": str(amount),
            "currency": listing.currency,
            "minimum_profitable_price": str(floor),
            "commercial_minimum": str(commercial_minimum),
            "publication_state": listing.status,
        },
    )
    return commercial_pricing_row(listing)


def commercial_pricing_row(listing: MarketServiceListing) -> dict:
    metadata = listing.platform_metadata if isinstance(listing.platform_metadata, dict) else {}
    record = metadata.get("commercial_pricing") if isinstance(metadata.get("commercial_pricing"), dict) else commercial_price_proposal(listing)
    price = record.get("public_price")
    blockers = list(record.get("blockers") or [])
    prepared = bool(price not in (None, "") and _decimal(price) > 0 and not blockers)
    publication_recorded = bool(
        listing.status == MarketServiceListing.Status.PUBLISHED
        and listing.remote_listing_id
        and listing.remote_reference
        and listing.published_at is not None
    )
    return {
        "market": listing.marketplace.slug,
        "package_slug": listing.offering.slug,
        "display_name": listing.offering.display_name,
        "pricing_model": listing.offering.pricing_model,
        "currency": listing.offering.currency,
        "billing_unit": record.get("billing_unit") or _pricing_unit(listing.offering.pricing_model),
        "state": record.get("state") or "UNPREPARED",
        "source": record.get("source") or "AUTO_PROPOSAL",
        "minimum_profitable_price": record.get("minimum_profitable_price") or str(listing.offering.minimum_profitable_price),
        "commercial_minimum": record.get("commercial_minimum"),
        "public_price": price,
        "prepared": prepared,
        "owner_approved": record.get("source") == "OWNER_OVERRIDE",
        "blockers": blockers,
        "listing_status": listing.status,
        "catalog_listing_price_field": str(listing.published_price),
        "published_price": str(listing.published_price) if publication_recorded else None,
        "publication_recorded": publication_recorded,
        "remote_publication_present": bool(listing.remote_listing_id or listing.remote_reference),
        "external_mutation_allowed": False,
    }


def priority_channel_commercial_pricing_snapshot() -> dict:
    package_slugs = [spec.slug for spec in PACKAGE_SPECS]
    listings = {
        row.offering.slug: row
        for row in MarketServiceListing.objects.select_related("offering", "marketplace").filter(offering__slug__in=package_slugs)
    }
    rows = [commercial_pricing_row(listings[spec.slug]) for spec in PACKAGE_SPECS if spec.slug in listings]
    return {
        "section": "priority-channel-commercial-pricing",
        "rows": rows,
        "meta": {
            "version": COMMERCIAL_PRICING_VERSION,
            "total_packages": len(PACKAGE_SPECS),
            "priced_packages": sum(1 for row in rows if row["prepared"]),
            "owner_approved_packages": sum(1 for row in rows if row["owner_approved"]),
            "blocked_packages": sum(1 for row in rows if row["blockers"]),
            "published_prices": sum(1 for row in rows if row["published_price"] is not None),
            "external_mutation_allowed": False,
            "truth": "Commercial prices are local launch proposals or owner-approved drafts. The existing listing price field is not publication evidence; a published price is reported only with persisted published listing and remote evidence.",
        },
    }
