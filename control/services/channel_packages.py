from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import os

from django.db import transaction

from control.models import MarketServiceListing, Marketplace, ServiceOffering
from control.services.profit_brain import GrowthStage, UtilizationState, recommend_price
from control.services.seller_services import sync_candidate_service_offerings
from markets.revenue_catalog import bootstrap_revenue_market_catalog


PACKAGE_CATALOG_VERSION = 1


@dataclass(frozen=True)
class ChannelPackageSpec:
    slug: str
    market: str
    operation: str
    display_name: str
    description: str
    pricing_model: str
    package_type: str
    execution_placement: str
    buyer_inputs: tuple[str, ...]
    deliverables: tuple[str, ...]
    sales_copy: str
    requires_external_execution_cost: bool = False


PACKAGE_SPECS = (
    ChannelPackageSpec(
        slug="contra-research-report",
        market="contra",
        operation="research_report",
        display_name="Research & Competitor Intelligence Report",
        description="Structured research, competitor analysis and decision-ready recommendations delivered as a polished report.",
        pricing_model=ServiceOffering.PricingModel.FIXED_PROJECT,
        package_type="CONTRA_SERVICE",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("research_question", "industry_or_market", "preferred_scope"),
        deliverables=("research_report", "source_summary", "key_recommendations"),
        sales_copy="Turn a business question into a concise evidence-backed research report with clear recommendations.",
    ),
    ChannelPackageSpec(
        slug="contra-data-analysis",
        market="contra",
        operation="data_analysis_report",
        display_name="Data Analysis & Insight Report",
        description="Analysis of supplied business data with findings, trends and a decision-ready report.",
        pricing_model=ServiceOffering.PricingModel.FIXED_PROJECT,
        package_type="CONTRA_SERVICE",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("dataset", "business_questions"),
        deliverables=("analysis_report", "findings", "recommendations"),
        sales_copy="Convert supplied data into useful findings, trends and a professional management-ready analysis.",
    ),
    ChannelPackageSpec(
        slug="contra-spreadsheet-report",
        market="contra",
        operation="spreadsheet_report",
        display_name="Professional Spreadsheet Report",
        description="A structured spreadsheet deliverable with clean presentation and independently reopen-tested output.",
        pricing_model=ServiceOffering.PricingModel.FIXED_PROJECT,
        package_type="CONTRA_SERVICE",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("source_data", "report_requirements"),
        deliverables=("xlsx_report",),
        sales_copy="Get a clean, professional spreadsheet report built from your source data and requirements.",
    ),
    ChannelPackageSpec(
        slug="contra-content-package",
        market="contra",
        operation="content_package",
        display_name="Business Content Package",
        description="A focused content package built to an approved brief and independently QA checked.",
        pricing_model=ServiceOffering.PricingModel.FIXED_PROJECT,
        package_type="CONTRA_SERVICE",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("brief", "audience", "brand_guidance"),
        deliverables=("content_package",),
        sales_copy="Turn your brief into a polished business content package ready for review and use.",
    ),
    ChannelPackageSpec(
        slug="contra-presentation",
        market="contra",
        operation="presentation_create",
        display_name="Professional Presentation Deck",
        description="A structured presentation deck produced from supplied content, goals and audience requirements.",
        pricing_model=ServiceOffering.PricingModel.FIXED_PROJECT,
        package_type="CONTRA_SERVICE",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("brief", "source_content", "audience"),
        deliverables=("pptx_deck",),
        sales_copy="Convert your source material into a clear, professional presentation deck.",
    ),
    ChannelPackageSpec(
        slug="rapidapi-json-to-csv",
        market="rapidapi",
        operation="json_to_csv",
        display_name="JSON to CSV API",
        description="Deterministic JSON-to-CSV conversion for structured payloads.",
        pricing_model=ServiceOffering.PricingModel.PER_CALL,
        package_type="RAPIDAPI_API",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("json_payload",),
        deliverables=("csv_output",),
        sales_copy="Convert structured JSON payloads to clean CSV output through a simple API call.",
    ),
    ChannelPackageSpec(
        slug="rapidapi-csv-normalize",
        market="rapidapi",
        operation="csv_normalize",
        display_name="CSV Normalization API",
        description="Normalize CSV structure and return a clean deterministic output.",
        pricing_model=ServiceOffering.PricingModel.PER_CALL,
        package_type="RAPIDAPI_API",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("csv_input",),
        deliverables=("normalized_csv",),
        sales_copy="Normalize inconsistent CSV data into a cleaner, predictable structure through one API call.",
    ),
    ChannelPackageSpec(
        slug="rapidapi-tabular-convert",
        market="rapidapi",
        operation="tabular_convert",
        display_name="Tabular Conversion API",
        description="Convert supported tabular formats into a normalized target format.",
        pricing_model=ServiceOffering.PricingModel.PER_CALL,
        package_type="RAPIDAPI_API",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("tabular_input", "target_format"),
        deliverables=("converted_table",),
        sales_copy="Convert supported tabular data into the target format with deterministic processing.",
    ),
    ChannelPackageSpec(
        slug="rapidapi-tabular-normalize",
        market="rapidapi",
        operation="tabular_normalize",
        display_name="Tabular Normalization API",
        description="Normalize tabular data for consistent downstream processing.",
        pricing_model=ServiceOffering.PricingModel.PER_CALL,
        package_type="RAPIDAPI_API",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("tabular_input",),
        deliverables=("normalized_table",),
        sales_copy="Standardize tabular data into a cleaner shape for downstream workflows.",
    ),
    ChannelPackageSpec(
        slug="rapidapi-image-resize",
        market="rapidapi",
        operation="image_resize",
        display_name="Image Resize API",
        description="Bounded image resize processing with output validation.",
        pricing_model=ServiceOffering.PricingModel.PER_CALL,
        package_type="RAPIDAPI_API",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("image", "width", "height"),
        deliverables=("resized_image",),
        sales_copy="Resize images to requested dimensions through a bounded, validated API workflow.",
    ),
    ChannelPackageSpec(
        slug="rapidapi-image-convert",
        market="rapidapi",
        operation="image_convert",
        display_name="Image Format Conversion API",
        description="Bounded image format conversion with output validation.",
        pricing_model=ServiceOffering.PricingModel.PER_CALL,
        package_type="RAPIDAPI_API",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("image", "target_format"),
        deliverables=("converted_image",),
        sales_copy="Convert supported image formats through a simple validated API workflow.",
    ),
    ChannelPackageSpec(
        slug="apify-website-data-extractor",
        market="apify-store",
        operation="public_web_extract",
        display_name="Website Data Extractor Actor",
        description="Prepared Actor package for bounded public website extraction with execution placed on Apify infrastructure.",
        pricing_model=ServiceOffering.PricingModel.PER_UNIT,
        package_type="APIFY_ACTOR",
        execution_placement="APIFY",
        buyer_inputs=("public_url", "extraction_scope"),
        deliverables=("structured_dataset", "run_summary"),
        sales_copy="Extract structured public web data into a reusable dataset with bounded execution and clear run output.",
        requires_external_execution_cost=True,
    ),
    ChannelPackageSpec(
        slug="lemon-research-product",
        market="lemon-squeezy",
        operation="research_report",
        display_name="On-Demand Research Report",
        description="One-time direct purchase for a structured research and recommendation report.",
        pricing_model=ServiceOffering.PricingModel.FIXED_PROJECT,
        package_type="LEMON_PRODUCT",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("research_question", "scope"),
        deliverables=("research_report", "recommendations"),
        sales_copy="Purchase a focused research report built around your business question and requested scope.",
    ),
    ChannelPackageSpec(
        slug="lemon-data-analysis-product",
        market="lemon-squeezy",
        operation="data_analysis_report",
        display_name="On-Demand Data Analysis",
        description="One-time direct purchase for analysis of supplied business data.",
        pricing_model=ServiceOffering.PricingModel.FIXED_PROJECT,
        package_type="LEMON_PRODUCT",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("dataset", "questions"),
        deliverables=("analysis_report", "recommendations"),
        sales_copy="Purchase a structured analysis of your supplied dataset with findings and recommendations.",
    ),
    ChannelPackageSpec(
        slug="lemon-content-product",
        market="lemon-squeezy",
        operation="content_package",
        display_name="Business Content Package",
        description="One-time direct purchase for a defined business content package.",
        pricing_model=ServiceOffering.PricingModel.FIXED_PROJECT,
        package_type="LEMON_PRODUCT",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("brief", "audience", "brand_guidance"),
        deliverables=("content_package",),
        sales_copy="Purchase a focused business content package created from your brief and audience requirements.",
    ),
    ChannelPackageSpec(
        slug="lemon-seo-subscription",
        market="lemon-squeezy",
        operation="seo_content_audit",
        display_name="Recurring SEO Content Review",
        description="Prepared subscription product for recurring SEO content review and recommendations.",
        pricing_model=ServiceOffering.PricingModel.SUBSCRIPTION,
        package_type="LEMON_SUBSCRIPTION",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("site_or_content_scope", "goals"),
        deliverables=("seo_audit", "recommendations"),
        sales_copy="Receive a recurring SEO content review with prioritized recommendations for improvement.",
    ),
    ChannelPackageSpec(
        slug="lemon-presentation-product",
        market="lemon-squeezy",
        operation="presentation_create",
        display_name="Presentation Deck Creation",
        description="One-time direct purchase for a structured professional presentation deck.",
        pricing_model=ServiceOffering.PricingModel.FIXED_PROJECT,
        package_type="LEMON_PRODUCT",
        execution_placement="WEBDOCK_LIGHT",
        buyer_inputs=("brief", "source_content", "audience"),
        deliverables=("pptx_deck",),
        sales_copy="Purchase a professional presentation deck created from your brief and source material.",
    ),
)


PRIORITY_MARKETS = tuple(dict.fromkeys(spec.market for spec in PACKAGE_SPECS))


def _decimal(value, default="0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _pricing_assumptions(market: Marketplace) -> dict:
    try:
        profile = market.integration_profile
    except Exception:
        profile = None
    evidence = profile.evidence if profile is not None and isinstance(profile.evidence, dict) else {}
    truth = evidence.get("catalog_truth") if isinstance(evidence.get("catalog_truth"), dict) else {}
    economics = truth.get("economics") if isinstance(truth.get("economics"), dict) else {}

    raw_percentage = economics.get("percentage_fee_rate")
    percentage_known = raw_percentage not in (None, "")
    percentage = _decimal(raw_percentage) if percentage_known else _decimal(os.getenv("MARKET_UNVERIFIED_FEE_RESERVE_RATE", "0.30"))

    raw_payout = economics.get("payout_cost_rate")
    payout_known = raw_payout not in (None, "")
    payout = _decimal(raw_payout) if payout_known else _decimal(os.getenv("MARKET_UNVERIFIED_PAYOUT_RESERVE_RATE", "0.05"))

    fx = _decimal(economics.get("fx_cost_rate"), "0")
    chargeback = _decimal(economics.get("chargeback_reserve_rate"), "0")
    fixed = _decimal(economics.get("fixed_transaction_fee"), "0")
    external = economics.get("external_execution_cost_usd")
    external_known = external not in (None, "")

    variable_rate = percentage + payout + fx + chargeback
    valid = all(value >= 0 for value in (percentage, payout, fx, chargeback, fixed)) and variable_rate < Decimal("0.95")

    return {
        "known_marketplace_fee_rate": str(_decimal(raw_percentage)) if percentage_known else None,
        "percentage_fee_verified": percentage_known,
        "payout_cost_verified": payout_known,
        "pricing_variable_rate_used": str(variable_rate),
        "fixed_transaction_fee": str(fixed),
        "external_execution_cost_usd": None if not external_known else str(_decimal(external)),
        "external_execution_cost_verified": external_known,
        "economics_verified": bool(economics.get("verified") is True),
        "valid": valid,
    }


def _price_for_package(base: ServiceOffering, market: Marketplace, spec: ChannelPackageSpec) -> tuple[Decimal, Decimal, list[str], dict]:
    assumptions = _pricing_assumptions(market)
    blockers: list[str] = []
    if not assumptions["valid"]:
        blockers.append("PACKAGE_PRICING_ASSUMPTIONS_INVALID")
    if spec.requires_external_execution_cost and not assumptions["external_execution_cost_verified"]:
        blockers.append("EXTERNAL_EXECUTION_COST_PROFILE_NOT_PROVEN")
    if blockers:
        return Decimal("0"), Decimal("0"), blockers, assumptions

    total_expected_cost = (
        _decimal(base.expected_genx_cost)
        + _decimal(base.expected_external_cost)
        + _decimal(base.expected_operational_cost)
        + _decimal(assumptions["fixed_transaction_fee"])
    )
    if assumptions["external_execution_cost_verified"]:
        total_expected_cost += _decimal(assumptions["external_execution_cost_usd"])

    recommendation = recommend_price(
        total_expected_cost=total_expected_cost,
        advertised_budget=None,
        competitive_price=None,
        fee_rate=_decimal(assumptions["pricing_variable_rate_used"]),
        utilization_state=UtilizationState.PARTIALLY_IDLE,
        growth_stage=GrowthStage.BOOTSTRAP,
    )
    return recommendation.offered_price, recommendation.minimum_profitable_price, blockers, assumptions


def _package_manifest(spec: ChannelPackageSpec, assumptions: dict, pricing_blockers: list[str]) -> dict:
    return {
        "catalog_version": PACKAGE_CATALOG_VERSION,
        "package_slug": spec.slug,
        "package_type": spec.package_type,
        "market": spec.market,
        "operation": spec.operation,
        "execution_placement": spec.execution_placement,
        "buyer_inputs": list(spec.buyer_inputs),
        "deliverables": list(spec.deliverables),
        "sales_copy": spec.sales_copy,
        "pricing_assumptions": assumptions,
        "pricing_blockers": list(pricing_blockers),
        "external_mutation_allowed": False,
        "publication_state": "DRAFT_ONLY",
    }


def _merged_channel_package_metadata(existing, manifest: dict) -> dict:
    value = dict(existing) if isinstance(existing, dict) else {}
    value["channel_package"] = manifest
    return value


def _offering_catalog_values(base: ServiceOffering, spec: ChannelPackageSpec, price: Decimal, minimum: Decimal, assumptions: dict, manifest: dict) -> dict:
    known_market_fee = assumptions["known_marketplace_fee_rate"]
    return {
        "display_name": spec.display_name,
        "description": spec.description,
        "capability": base.capability,
        "operation": base.operation,
        "worker_class": base.worker_class,
        "pricing_model": spec.pricing_model,
        "currency": "USD",
        "advertised_price": price,
        "minimum_profitable_price": minimum,
        "platform_fee_rate": _decimal(known_market_fee) if known_market_fee is not None else Decimal("0"),
        "expected_genx_cost": base.expected_genx_cost,
        "max_genx_credits": base.max_genx_credits,
        "expected_external_cost": base.expected_external_cost,
        "expected_operational_cost": base.expected_operational_cost,
        "expected_minutes": base.expected_minutes,
        "sla_minutes": base.sla_minutes,
        "input_schema": base.input_schema,
        "output_schema": base.output_schema,
        "terms_metadata": _merged_channel_package_metadata(base.terms_metadata, manifest),
    }


def _refresh_inactive_offering(offering: ServiceOffering, values: dict) -> bool:
    if offering.enabled or offering.accepting_orders:
        return False
    changed = []
    for field, value in values.items():
        if getattr(offering, field) != value:
            setattr(offering, field, value)
            changed.append(field)
    if changed:
        offering.save(update_fields=[*changed, "updated_at"])
    return bool(changed)


def _listing_catalog_values(spec: ChannelPackageSpec, price: Decimal, manifest: dict, existing_metadata=None) -> dict:
    return {
        "published_price": price,
        "currency": "USD",
        "pricing_model": spec.pricing_model,
        "platform_metadata": _merged_channel_package_metadata(existing_metadata, manifest),
    }


def _refresh_unpublished_listing(listing: MarketServiceListing, values: dict) -> bool:
    if listing.status == MarketServiceListing.Status.PUBLISHED or listing.remote_listing_id or listing.remote_reference:
        return False
    changed = []
    for field, value in values.items():
        if getattr(listing, field) != value:
            setattr(listing, field, value)
            changed.append(field)
    if changed:
        listing.save(update_fields=[*changed, "updated_at"])
    return bool(changed)


@transaction.atomic
def sync_priority_channel_packages() -> dict[str, int]:
    bootstrap_revenue_market_catalog()
    sync_candidate_service_offerings()

    offerings_created = listings_created = offerings_updated = listings_updated = unchanged = 0
    active_offerings_preserved = published_listings_preserved = 0
    for spec in PACKAGE_SPECS:
        market = Marketplace.objects.get(slug=spec.market)
        base = ServiceOffering.objects.get(slug=spec.operation.replace("_", "-"))
        price, minimum, pricing_blockers, assumptions = _price_for_package(base, market, spec)
        manifest = _package_manifest(spec, assumptions, pricing_blockers)
        offering_values = _offering_catalog_values(base, spec, price, minimum, assumptions, manifest)

        offering, offering_created = ServiceOffering.objects.get_or_create(
            slug=spec.slug,
            defaults={
                **offering_values,
                "proof_evidence": dict(base.proof_evidence or {}),
                "enabled": False,
                "accepting_orders": False,
                "proof_state": base.proof_state,
            },
        )
        offerings_created += int(offering_created)
        offering_updated = False
        if not offering_created:
            offering_updated = _refresh_inactive_offering(offering, offering_values)
            offerings_updated += int(offering_updated)
            active_offerings_preserved += int((offering.enabled or offering.accepting_orders) and not offering_updated)

        listing_values = _listing_catalog_values(spec, price, manifest)
        listing, listing_created = MarketServiceListing.objects.get_or_create(
            offering=offering,
            marketplace=market,
            defaults={
                "status": MarketServiceListing.Status.DRAFT,
                **listing_values,
            },
        )
        listings_created += int(listing_created)
        listing_updated = False
        if not listing_created:
            listing_values = _listing_catalog_values(spec, price, manifest, listing.platform_metadata)
            listing_updated = _refresh_unpublished_listing(listing, listing_values)
            listings_updated += int(listing_updated)
            published_listings_preserved += int(
                (listing.status == MarketServiceListing.Status.PUBLISHED or bool(listing.remote_listing_id) or bool(listing.remote_reference))
                and not listing_updated
            )

        unchanged += int(
            not offering_created
            and not listing_created
            and not offering_updated
            and not listing_updated
        )

    return {
        "catalog_version": PACKAGE_CATALOG_VERSION,
        "packages": len(PACKAGE_SPECS),
        "offerings_created": offerings_created,
        "listings_created": listings_created,
        "offerings_updated": offerings_updated,
        "listings_updated": listings_updated,
        "active_offerings_preserved": active_offerings_preserved,
        "published_listings_preserved": published_listings_preserved,
        "unchanged": unchanged,
    }


def priority_channel_package_snapshot() -> dict:
    listings = {
        listing.offering.slug: listing
        for listing in MarketServiceListing.objects.filter(
            marketplace__slug__in=PRIORITY_MARKETS,
            offering__slug__in=[spec.slug for spec in PACKAGE_SPECS],
        ).select_related("offering", "marketplace")
    }
    rows = []
    for spec in PACKAGE_SPECS:
        listing = listings.get(spec.slug)
        offering = listing.offering if listing is not None else None
        manifest = {}
        if listing is not None and isinstance(listing.platform_metadata, dict):
            manifest = listing.platform_metadata.get("channel_package") or {}
        pricing_blockers = list(manifest.get("pricing_blockers") or []) if isinstance(manifest, dict) else []
        prepared = bool(
            listing is not None
            and offering is not None
            and listing.status in {MarketServiceListing.Status.DRAFT, MarketServiceListing.Status.READY}
            and not listing.remote_listing_id
            and not listing.remote_reference
            and manifest.get("external_mutation_allowed") is False
        )
        rows.append({
            "market": spec.market,
            "package_slug": spec.slug,
            "display_name": spec.display_name,
            "package_type": spec.package_type,
            "operation": spec.operation,
            "pricing_model": spec.pricing_model,
            "execution_placement": spec.execution_placement,
            "prepared": prepared,
            "price_ready": prepared and not pricing_blockers and bool(listing and listing.published_price > 0),
            "shadow_price": None if listing is None else str(listing.published_price),
            "currency": None if listing is None else listing.currency,
            "listing_status": None if listing is None else listing.status,
            "remote_listing_recorded": bool(listing and (listing.remote_listing_id or listing.remote_reference)),
            "offering_proof_state": None if offering is None else offering.proof_state,
            "offering_enabled": False if offering is None else bool(offering.enabled),
            "accepting_orders": False if offering is None else bool(offering.accepting_orders),
            "pricing_blockers": pricing_blockers,
            "external_mutation_allowed": False,
            "sales_copy": spec.sales_copy,
            "buyer_inputs": list(spec.buyer_inputs),
            "deliverables": list(spec.deliverables),
        })
    published = sum(1 for row in rows if row["listing_status"] == MarketServiceListing.Status.PUBLISHED)
    return {
        "section": "priority-channel-packages",
        "rows": rows,
        "meta": {
            "catalog_version": PACKAGE_CATALOG_VERSION,
            "total_packages": len(rows),
            "prepared_packages": sum(1 for row in rows if row["prepared"]),
            "price_ready_packages": sum(1 for row in rows if row["price_ready"]),
            "published_packages": published,
            "external_mutation_allowed": False,
            "truth": "Package catalog sync is local and performs no external publication. Published counts are derived from persisted listing state and remote publication evidence is preserved rather than overwritten.",
        },
    }
