from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from control.models import (
    Alert,
    AuditEvent,
    CapabilityMonetization,
    DistributionCampaign,
    GenXCall,
    InboundOrder,
    InternalOpportunity,
    Job,
    JobScore,
    Marketplace,
    MarketServiceListing,
    ProductCandidate,
    ServiceOffering,
    SystemSetting,
)
from control.services.autonomy import AutonomyMode, current_mode
from control.services.profit_brain import (
    capture_capacity,
    evaluate_opportunity,
    persist_opportunity_decision,
)
from planning.models import WorkPlan
from workers.registry import all_specs

POLICY_KEY = "owned_revenue.product_factory.v1"
DEFAULT_POLICY = {
    "version": 1,
    "enabled": False,
    "daily_genx_credit_ceiling": "25",
    "per_product_credit_ceiling": "8",
    "maximum_simultaneous_internal_jobs": 1,
    "minimum_expected_margin": "0.40",
    "minimum_confidence": "0.65",
    "maximum_experimental_credit_budget": "5",
    "maximum_unsold_inventory_count": 10,
    "stop_loss_currency": "10.00",
    "minimum_external_priority": 70,
}


@dataclass(frozen=True)
class ProductBlueprint:
    slug: str
    product_class: str
    title: str
    target_buyer: str
    worker_class: str
    operation: str
    channels: tuple[str, ...]
    currency: str
    price: Decimal
    expected_sales: int
    expected_cost: Decimal
    confidence: Decimal
    max_credits: Decimal
    prompt: str


BLUEPRINTS = (
    ProductBlueprint("original-business-social-template-pack", "IMAGE_DESIGN", "Original business social template pack", "Small businesses needing reusable on-brand social layouts", "image_product", "image_generate_product_asset", ("lemon-squeezy", "payhip", "gumroad"), "USD", Decimal("29"), 3, Decimal("12"), Decimal("0.68"), Decimal("6"), "Create an original rights-safe coordinated set of clean business social backgrounds and layout-ready visual assets with no logos, trademarks, celebrity likenesses, copyrighted characters, or testimonial claims."),
    ProductBlueprint("business-research-planning-pack", "TEXT_DOCUMENT", "Business research and planning pack", "Independent operators who need structured planning worksheets and evidence-led research templates", "content_copy", "content_package", ("lemon-squeezy", "payhip"), "USD", Decimal("24"), 4, Decimal("8"), Decimal("0.75"), Decimal("4"), "Create an original differentiated business research and planning template pack with buyer instructions, checklists, worksheets, and a clear practical use case. Avoid generic filler."),
    ProductBlueprint("structured-extraction-micro-api", "MICRO_API", "Structured extraction micro-API package", "Developers needing reliable bounded text-to-structure utilities", "technical_documentation", "technical_documentation", ("rapidapi",), "USD", Decimal("19"), 10, Decimal("20"), Decimal("0.66"), Decimal("7"), "Create the publication-ready specification, examples, input/output contracts, QA cases, and support documentation for a narrowly differentiated structured extraction API backed by existing reliable workers."),
    ProductBlueprint("bounded-public-data-actor", "APIFY_ACTOR", "Bounded public-data Actor candidate", "Businesses needing a policy-compliant narrow public-data workflow", "technical_documentation", "technical_documentation", ("apify-store",), "USD", Decimal("39"), 5, Decimal("30"), Decimal("0.62"), Decimal("8"), "Create a publication-ready Apify Actor product specification and implementation package for a narrow legitimate public-data use case. Require robots/access compliance, no paywall bypass, bounded runs, and execution only on Apify."),
    ProductBlueprint("owned-product-listing-growth-pack", "MARKETING_ASSET", "Owned-product listing growth pack", "AmarktAI product listings that lack clear commercial copy and demo assets", "content_copy", "content_package", ("owner-review",), "USD", Decimal("20"), 2, Decimal("6"), Decimal("0.72"), Decimal("3"), "Create an owner-reviewed publication package for a real AmarktAI product: listing copy, FAQ, demo outline, ethical social posts, and SEO-supporting original informational content. Do not fabricate reviews or engagement."),
)


def load_policy() -> dict[str, Any]:
    setting = SystemSetting.objects.filter(key=POLICY_KEY).first()
    policy = deepcopy(DEFAULT_POLICY)
    if setting and isinstance(setting.value, dict):
        for key in policy:
            if key in setting.value:
                policy[key] = setting.value[key]
    return policy


@transaction.atomic
def update_policy(values: dict[str, Any], *, actor: str) -> dict[str, Any]:
    policy = load_policy()
    allowed = set(DEFAULT_POLICY)
    if set(values) - allowed:
        raise ValueError("unsupported_factory_policy_field")
    policy.update(values)
    numeric_positive = ("daily_genx_credit_ceiling", "per_product_credit_ceiling", "maximum_experimental_credit_budget", "stop_loss_currency")
    for key in numeric_positive:
        value = Decimal(str(policy[key]))
        if value < 0:
            raise ValueError("factory_budget_must_be_nonnegative")
        policy[key] = str(value)
    for key in ("minimum_expected_margin", "minimum_confidence"):
        value = Decimal(str(policy[key]))
        if not Decimal("0") <= value <= Decimal("1"):
            raise ValueError("factory_probability_out_of_range")
        policy[key] = str(value)
    for key in ("maximum_simultaneous_internal_jobs", "maximum_unsold_inventory_count"):
        policy[key] = max(0, int(policy[key]))
    policy["enabled"] = bool(policy["enabled"])
    SystemSetting.objects.update_or_create(key=POLICY_KEY, defaults={"value": policy, "sensitive": False})
    AuditEvent.objects.create(event_type="product_factory.policy_updated", actor=str(actor)[:120], metadata={"enabled": policy["enabled"], "daily_genx_credit_ceiling": policy["daily_genx_credit_ceiling"], "inventory_cap": policy["maximum_unsold_inventory_count"]})
    return policy


def _offering_defaults(spec, operation: str) -> dict[str, Any]:
    genx = spec.requires_genx
    code = spec.worker_class.startswith("code_")
    price = Decimal("100") if code else Decimal("35") if genx else Decimal("20")
    genx_cost = Decimal("5") if genx else Decimal("0")
    external = Decimal("4") if spec.worker_class == "public_web_data" else Decimal("0")
    return {
        "display_name": f"{operation.replace('_', ' ').title()}",
        "description": spec.description,
        "capability": spec.worker_class,
        "operation": operation,
        "worker_class": spec.worker_class,
        "pricing_model": ServiceOffering.PricingModel.FIXED_PROJECT,
        "currency": "USD",
        "advertised_price": price,
        "minimum_profitable_price": genx_cost + external + Decimal("5"),
        "platform_fee_rate": Decimal("0"),
        "expected_genx_cost": genx_cost,
        "max_genx_credits": Decimal("8") if genx else Decimal("0"),
        "expected_external_cost": external,
        "expected_operational_cost": Decimal("2"),
        "expected_minutes": 60 if code else 30,
        "input_schema": {"type": "object", "operation": operation, "buyer_inputs_required": True},
        "output_schema": {"type": "artifact", "qa_profile": spec.qa_profile},
        "terms_metadata": {"rights_safe": True, "private_client_reuse": False},
        "enabled": False,
        "accepting_orders": False,
        "proof_state": ServiceOffering.ProofState.UNPROVEN if genx else ServiceOffering.ProofState.SOURCE_PROVEN,
    }


def _channels(spec, operation: str) -> list[str]:
    channels = ["paystack", "lemon-squeezy"]
    if spec.worker_class in {"structured_data", "advanced_structured_data", "documents", "media"}:
        channels.append("rapidapi")
    if spec.worker_class == "public_web_data":
        channels.append("apify-store")
    if operation.startswith("image_"):
        channels.extend(["payhip", "gumroad"])
    return list(dict.fromkeys(channels))


@transaction.atomic
def sync_capability_monetization_matrix() -> dict[str, int]:
    created = updated = 0
    for spec in all_specs():
        for operation in spec.operations:
            slug = f"cap-{operation}"[:50]
            offering, was_created = ServiceOffering.objects.update_or_create(slug=slug, defaults=_offering_defaults(spec, operation))
            price = offering.advertised_price
            cost = offering.expected_genx_cost + offering.expected_external_cost + offering.expected_operational_cost
            margin = Decimal("0") if price <= 0 else (price - cost) / price
            _mapping, mapping_created = CapabilityMonetization.objects.update_or_create(
                worker_class=spec.worker_class,
                operation=operation,
                commercial_deliverable=offering.display_name,
                defaults={
                    "genx_task_class": operation if spec.requires_genx else "",
                    "offering": offering,
                    "channels": _channels(spec, operation),
                    "expected_price": price,
                    "estimated_cost": cost,
                    "expected_margin": margin,
                    "readiness": "SOURCE_PROVEN" if not spec.requires_genx else "LIVE_MODEL_PROOF_REQUIRED",
                    "input_schema": offering.input_schema,
                    "output_schema": offering.output_schema,
                    "qa_profile": spec.qa_profile,
                },
            )
            created += int(was_created or mapping_created)
            updated += int(not (was_created or mapping_created))
    return {"created": created, "updated": updated, "capabilities": CapabilityMonetization.objects.count()}


def _factory_market() -> Marketplace:
    market, _ = Marketplace.objects.get_or_create(slug="owned-products", defaults={"display_name": "AmarktAI Owned Products", "status": Marketplace.Status.WATCH_ONLY, "enabled": False, "payout_ready": False, "payment_model": "INTERNAL_OPPORTUNITY"})
    return market


@transaction.atomic
def generate_product_candidates() -> dict[str, int]:
    created = 0
    for blueprint in BLUEPRINTS:
        gross = blueprint.price * blueprint.expected_sales
        net = gross - blueprint.expected_cost
        margin = net / gross if gross else Decimal("0")
        unit_cost = blueprint.expected_cost / Decimal(max(blueprint.expected_sales, 1))
        offering, _ = ServiceOffering.objects.get_or_create(
            slug=f"owned-{blueprint.slug}"[:50],
            defaults={
                "display_name": blueprint.title,
                "description": f"Original AmarktAI-owned {blueprint.product_class.casefold().replace('_', ' ')} product. Publication remains owner/proof gated.",
                "capability": blueprint.product_class,
                "operation": blueprint.operation,
                "worker_class": blueprint.worker_class,
                "pricing_model": ServiceOffering.PricingModel.FIXED_PROJECT,
                "currency": blueprint.currency,
                "advertised_price": blueprint.price,
                "minimum_profitable_price": unit_cost * Decimal("1.25"),
                "expected_genx_cost": unit_cost,
                "max_genx_credits": blueprint.max_credits,
                "expected_external_cost": Decimal("0"),
                "expected_operational_cost": Decimal("0"),
                "expected_minutes": 5,
                "input_schema": {"type": "object", "buyer_inputs_required": False},
                "output_schema": {"type": "owned_digital_inventory", "product_class": blueprint.product_class},
                "terms_metadata": {"owned_product": True, "original_required": True, "client_material_reuse": False},
                "enabled": False,
                "accepting_orders": False,
                "proof_state": ServiceOffering.ProofState.UNPROVEN,
            },
        )
        product, was_created = ProductCandidate.objects.get_or_create(
            slug=blueprint.slug,
            defaults={
                "product_class": blueprint.product_class,
                "title": blueprint.title,
                "target_buyer": blueprint.target_buyer,
                "currency": blueprint.currency,
                "offering": offering,
                "state": ProductCandidate.State.PRODUCT_CANDIDATE,
                "intended_channels": list(blueprint.channels),
                "suggested_price": blueprint.price,
                "expected_sales": blueprint.expected_sales,
                "expected_gross": gross,
                "expected_cost": blueprint.expected_cost,
                "expected_net": net,
                "expected_margin": margin,
                "confidence": blueprint.confidence,
                "max_genx_credits": blueprint.max_credits,
                "max_inventory_quantity": 1,
                "commercial_copy": {
                    "working_prompt": blueprint.prompt,
                    "suggested_channels": list(blueprint.channels),
                    "price": str(blueprint.price),
                    "currency": blueprint.currency,
                },
                "rights_evidence": {"original_required": True, "client_material_reuse": False, "trademarks_prohibited": True},
                "review_at": timezone.now() + timedelta(days=30),
            },
        )
        if product.offering_id is None:
            product.offering = offering
            product.save(update_fields=["offering", "updated_at"])
        if was_created:
            InternalOpportunity.objects.create(product=product, opportunity_type=blueprint.product_class, priority=80, expected_value=net * blueprint.confidence, deduplication_key=f"build:{blueprint.slug}:v1")
            created += 1
    return {"created": created, "total": ProductCandidate.objects.count()}


@transaction.atomic
def generate_internal_opportunities() -> int:
    """Create bounded replenishment candidates only from authoritative sales evidence."""
    created = 0
    products = ProductCandidate.objects.select_for_update().filter(
        state__in=[ProductCandidate.State.READY_TO_PUBLISH, ProductCandidate.State.PUBLISHED],
        sales__gt=0,
    )
    for product in products:
        if product.inventory_quantity >= product.max_inventory_quantity or product.net_profit <= 0:
            continue
        _opportunity, was_created = InternalOpportunity.objects.get_or_create(
            deduplication_key=f"replenish:{product.slug}:sale-{product.sales}:inventory-{product.inventory_quantity}",
            defaults={
                "product": product,
                "opportunity_type": f"REPLENISH_{product.product_class}"[:60],
                "priority": 90,
                "expected_value": max(product.net_profit, Decimal("0")),
            },
        )
        created += int(was_created)
    return created


def _refresh_product_roi(product: ProductCandidate, *, now=None) -> None:
    total_cost = product.cost_basis + product.publication_cost + product.promotion_cost
    product.net_profit = product.payout_received - total_cost
    product.return_on_production_cost = None if total_cost <= 0 else product.net_profit / total_cost
    if product.break_even_at is None and total_cost > 0 and product.payout_received >= total_cost:
        product.break_even_at = now or timezone.now()


@transaction.atomic
def record_owned_product_sale(*, offering: ServiceOffering, channel: str, event_key: str, gross: Decimal, refunded: bool = False) -> bool:
    """Persist provider-authoritative sale/refund metrics without calling them settled cash."""
    product = ProductCandidate.objects.select_for_update().filter(offering=offering).first()
    if product is None:
        return False
    evidence = dict(product.commercial_evidence or {})
    events = dict(evidence.get("sale_events") or {})
    target_state = "REFUNDED" if refunded else "PAID"
    previous = str(events.get(event_key) or "")
    if previous == target_state or (refunded and previous != "PAID"):
        return False
    now = timezone.now()
    if refunded:
        product.refunds += 1
    else:
        product.sales += 1
        product.inventory_quantity = max(0, product.inventory_quantity - 1)
        product.gross_revenue += Decimal(gross)
        product.first_sale_at = product.first_sale_at or now
    events[event_key] = target_state
    evidence["sale_events"] = events
    product.commercial_evidence = evidence
    _refresh_product_roi(product, now=now)
    product.save(update_fields=[
        "sales", "refunds", "inventory_quantity", "gross_revenue", "first_sale_at",
        "commercial_evidence", "net_profit", "return_on_production_cost", "break_even_at", "updated_at",
    ])
    campaign, _ = DistributionCampaign.objects.get_or_create(product=product, channel=channel)
    if not refunded:
        campaign.conversions += 1
        campaign.attributed_revenue += Decimal(gross)
        campaign.save(update_fields=["conversions", "attributed_revenue", "updated_at"])
    return True


@transaction.atomic
def record_owned_product_payout(*, payout) -> bool:
    """Update owned-product received-cash ROI from the canonical payout lifecycle."""
    order = InboundOrder.objects.select_related("listing__offering").filter(job_id=payout.job_id).first()
    if order is None:
        return False
    product = ProductCandidate.objects.select_for_update().filter(offering=order.listing.offering).first()
    if product is None:
        return False
    evidence = dict(product.commercial_evidence or {})
    payouts = dict(evidence.get("payout_events") or {})
    key = str(payout.id)
    previous = str(payouts.get(key) or "")
    if previous == payout.state:
        return False
    if payout.state == "SETTLED":
        product.payout_received += payout.net
    elif payout.state == "REVERSED" and previous == "SETTLED":
        product.payout_received -= payout.net
    else:
        return False
    payouts[key] = payout.state
    evidence["payout_events"] = payouts
    product.commercial_evidence = evidence
    _refresh_product_roi(product)
    product.save(update_fields=[
        "payout_received", "commercial_evidence", "net_profit", "return_on_production_cost", "break_even_at", "updated_at",
    ])
    return True


@transaction.atomic
def record_owned_product_publication(
    *, product_slug: str, channel: str, remote_listing_id: str, remote_reference: str, actor: str,
) -> ProductCandidate:
    """Reconcile an owner-performed external publication; never perform the mutation."""
    product = ProductCandidate.objects.select_for_update().select_related("offering").get(slug=product_slug)
    if product.state not in {ProductCandidate.State.READY_TO_PUBLISH, ProductCandidate.State.PUBLISHED}:
        raise ValueError("PRODUCT_NOT_READY_TO_PUBLISH")
    if channel not in set(product.intended_channels or []):
        raise ValueError("PRODUCT_CHANNEL_NOT_APPROVED")
    if not product.offering_id:
        raise ValueError("PRODUCT_OFFERING_MISSING")
    remote_listing_id = " ".join(str(remote_listing_id or "").split())[:255]
    remote_reference = " ".join(str(remote_reference or "").split())[:700]
    if not remote_listing_id or not remote_reference:
        raise ValueError("PRODUCT_PUBLICATION_EVIDENCE_REQUIRED")
    from control.services.integration_accounts import ensure_integration_profile

    profile = ensure_integration_profile(channel)
    now = timezone.now()
    listing, created = MarketServiceListing.objects.get_or_create(
        offering=product.offering,
        marketplace=profile.marketplace,
        defaults={
            "pricing_model": product.offering.pricing_model,
            "currency": product.currency,
            "published_price": product.suggested_price,
        },
    )
    if not created and listing.status == MarketServiceListing.Status.PUBLISHED:
        if listing.remote_listing_id != remote_listing_id or listing.remote_reference != remote_reference:
            raise ValueError("PRODUCT_PUBLICATION_IDEMPOTENCY_CONFLICT")
        return product
    listing.status = MarketServiceListing.Status.PUBLISHED
    listing.remote_listing_id = remote_listing_id
    listing.remote_reference = remote_reference
    listing.published_price = product.suggested_price
    listing.currency = product.currency
    listing.published_at = now
    listing.last_synced_at = now
    listing.platform_metadata = {
        **(listing.platform_metadata or {}),
        "owned_product": product.slug,
        "publication_evidence": {
            "mode": "OWNER_RECONCILED_REMOTE_PUBLICATION",
            "recorded_at": now.isoformat(),
            "external_mutation_performed_by_amarktai": False,
        },
    }
    listing.save()
    product.state = ProductCandidate.State.PUBLISHED
    product.published_at = product.published_at or now
    product.save(update_fields=["state", "published_at", "updated_at"])
    campaign, _ = DistributionCampaign.objects.get_or_create(product=product, channel=channel)
    campaign.status = "PUBLISHED"
    campaign.tracking_reference = remote_reference
    campaign.save(update_fields=["status", "tracking_reference", "updated_at"])
    AuditEvent.objects.create(
        event_type="product_factory.publication_reconciled",
        actor=str(actor)[:120],
        metadata={
            "product": product.slug,
            "channel": channel,
            "listing_id": str(listing.id),
            "external_mutation_performed": False,
            "revenue_recorded": False,
            "payout_truth_changed": False,
        },
    )
    return product


def _daily_internal_credits() -> Decimal:
    rows = GenXCall.objects.filter(job__internal_opportunity__isnull=False, created_at__date=timezone.localdate()).values_list("credits", "estimated_credits", "status", "requested_metadata")
    total = Decimal("0")
    for credits, estimate, status, metadata in rows:
        if str(status).upper() in {"FAILED", "CANCELLED"}:
            continue
        total += credits if str((metadata or {}).get("billing_truth") or "").upper() == "ACTUAL" else (credits or estimate)
    return total


def apply_stop_loss() -> int:
    policy = load_policy()
    threshold = Decimal(str(policy["stop_loss_currency"]))
    paused = 0
    for product in ProductCandidate.objects.exclude(state__in=[ProductCandidate.State.PAUSED, ProductCandidate.State.RETIRED]):
        total_spend = product.cost_basis + product.publication_cost + product.promotion_cost
        rejection_count = int((product.qa_evidence or {}).get("rejected_assets") or 0)
        produced_count = max(product.inventory_quantity + rejection_count, 0)
        excessive_rejection = produced_count >= 3 and Decimal(rejection_count) / Decimal(produced_count) >= Decimal("0.50")
        no_traction_loss = total_spend >= threshold and product.sales == 0
        if excessive_rejection or no_traction_loss or product.net_profit <= -threshold:
            product.state = ProductCandidate.State.PAUSED
            product.paused_reason = "QA_REJECTION_STOP_LOSS" if excessive_rejection else "NO_TRACTION_STOP_LOSS"
            product.save(update_fields=["state", "paused_reason", "updated_at"])
            Alert.objects.create(severity="WARN", alert_type="PRODUCT_FACTORY_STOP_LOSS", message=f"Product strategy {product.slug} was paused by bounded stop-loss evidence.", metadata={"product": product.slug, "reason": product.paused_reason})
            paused += 1
    return paused


@transaction.atomic
def admit_next_internal_opportunity() -> InternalOpportunity | None:
    policy = load_policy()
    if not policy["enabled"]:
        return None
    if current_mode() not in {AutonomyMode.LOW_RISK, AutonomyMode.FULL}:
        return None
    if _daily_internal_credits() >= Decimal(str(policy["daily_genx_credit_ceiling"])):
        return None
    active = InternalOpportunity.objects.filter(state__in=[InternalOpportunity.State.ECONOMICS_APPROVED, InternalOpportunity.State.QUEUED, InternalOpportunity.State.EXECUTING]).count()
    if active >= int(policy["maximum_simultaneous_internal_jobs"]):
        return None
    if ProductCandidate.objects.filter(inventory_quantity__gt=0, state__in=[ProductCandidate.State.QA_PASSED, ProductCandidate.State.READY_TO_PUBLISH, ProductCandidate.State.PUBLISHED]).count() >= int(policy["maximum_unsold_inventory_count"]):
        return None
    higher_priority = Job.objects.filter(state__in=[Job.State.AWARDED, Job.State.EXECUTING, Job.State.PAYOUT_PENDING]).exclude(internal_opportunity__isnull=False).exists()
    if higher_priority:
        return None
    opportunity = InternalOpportunity.objects.select_for_update().select_related("product").filter(state=InternalOpportunity.State.CANDIDATE).order_by("-expected_value", "created_at").first()
    if not opportunity:
        return None
    product = opportunity.product
    if product.expected_margin < Decimal(str(policy["minimum_expected_margin"])) or product.confidence < Decimal(str(policy["minimum_confidence"])):
        opportunity.state = InternalOpportunity.State.BLOCKED
        opportunity.reason_codes = ["FACTORY_ECONOMIC_FLOOR_NOT_MET"]
        opportunity.save(update_fields=["state", "reason_codes", "updated_at"])
        return None
    blueprint = next(row for row in BLUEPRINTS if row.slug == product.slug)
    per_product = min(product.max_genx_credits, Decimal(str(policy["per_product_credit_ceiling"])))
    if product.sales == 0:
        per_product = min(per_product, Decimal(str(policy["maximum_experimental_credit_budget"])))
    remaining_daily = Decimal(str(policy["daily_genx_credit_ceiling"])) - _daily_internal_credits()
    per_product = min(per_product, max(remaining_daily, Decimal("0")))
    if per_product <= 0:
        return None
    market = _factory_market()
    external_id = f"internal:{product.slug}:v1"
    job, _ = Job.objects.get_or_create(
        marketplace=market,
        external_id=external_id,
        defaults={
            "title": product.title,
            "task_class": blueprint.operation,
            "reward": product.expected_gross,
            "currency": "USD",
            "state": Job.State.AWARDED,
            "normalized_payload": {
                "source_type": "INTERNAL_OPPORTUNITY",
                "operation": blueprint.operation,
                "worker_class": blueprint.worker_class,
                "product_slug": product.slug,
                "rights_safe_original": True,
                "prompt": blueprint.prompt,
                "minimum_quality": "0.85" if blueprint.product_class == "IMAGE_DESIGN" else "0.80",
                "target_state": "READY_TO_PUBLISH",
            },
        },
    )
    expected_profit = product.expected_net
    JobScore.objects.update_or_create(
        job=job,
        defaults={
            "p_acquire": Decimal("1"),
            "p_accept": product.confidence,
            "p_payment": product.confidence,
            "expected_genx_cost": min(product.expected_cost, per_product),
            "expected_external_cost": Decimal("0"),
            "expected_cash": product.expected_gross * product.confidence,
            "expected_profit": expected_profit,
            "expected_profit_per_minute": expected_profit / Decimal("60"),
            "expected_profit_per_genx_credit": expected_profit / per_product if per_product else None,
            "expected_minutes": 60,
            "max_genx_credits": per_product,
            "decision": "INTERNAL_FACTORY_CANDIDATE",
            "reason_codes": [],
            "score_version": "product-factory-v1",
        },
    )
    capacity = capture_capacity(persist=True)
    economic = evaluate_opportunity(job, capacity=capacity, capability=blueprint.worker_class)
    persist_opportunity_decision(job, economic, capacity=capacity, allowed=economic.allowed, reason_codes=economic.reason_codes)
    if not economic.allowed:
        opportunity.state = InternalOpportunity.State.BLOCKED
        opportunity.reason_codes = list(economic.reason_codes)
        opportunity.job = job
        opportunity.save(update_fields=["state", "reason_codes", "job", "updated_at"])
        return None
    input_spec = {
        "operation": blueprint.operation,
        "prompt": blueprint.prompt,
        "rights_safe_original": True,
        "estimated_genx_credits": str(min(per_product, Decimal("2"))) if per_product else "0.25",
        "max_genx_call_credits": str(per_product or Decimal("1")),
        "minimum_quality": "0.85" if blueprint.product_class == "IMAGE_DESIGN" else "0.80",
        "allow_model_exploration": product.confidence >= Decimal("0.75"),
    }
    WorkPlan.objects.update_or_create(
        job=job,
        defaults={
            "worker_class": blueprint.worker_class,
            "operation": blueprint.operation,
            "input_spec": input_spec,
            "status": WorkPlan.Status.READY,
            "planner_version": "product-factory-v1",
            "reason_codes": [],
            "max_repair_attempts": 1,
            "minimum_quality": Decimal(input_spec["minimum_quality"]),
            "max_repair_cost": per_product,
            "escalation_policy": {"bounded": True, "economic_justification_required": True, "maximum_repairs": 1},
        },
    )
    product.job = job
    product.state = ProductCandidate.State.ECONOMICS_APPROVED
    product.save(update_fields=["job", "state", "updated_at"])
    opportunity.job = job
    opportunity.state = InternalOpportunity.State.ECONOMICS_APPROVED
    opportunity.save(update_fields=["job", "state", "updated_at"])
    AuditEvent.objects.create(event_type="product_factory.internal_opportunity_admitted", actor="profit-brain", metadata={"product": product.slug, "job_id": str(job.id), "expected_net": str(expected_profit), "max_genx_credits": str(per_product), "auto_publish": False})
    return opportunity


def _queue_internal_opportunity(opportunity: InternalOpportunity) -> bool:
    """Queue approved owned-product work behind all paid/customer work."""
    from control.queueing import queue
    from control.tasks import execute_work_plan_task

    plan = WorkPlan.objects.get(job=opportunity.job)
    try:
        queue("p7").enqueue(
            execute_work_plan_task,
            plan.id,
            job_id=f"product-factory:execute:{plan.id}:{plan.execution_attempts + 1}",
            result_ttl=86400,
            failure_ttl=604800,
        )
    except Exception as exc:  # noqa: BLE001 - Redis/RQ delivery is an external failure boundary
        AuditEvent.objects.create(
            severity="WARNING",
            event_type="product_factory.queue_failed",
            actor="product-factory",
            metadata={
                "product": opportunity.product.slug,
                "job_id": str(opportunity.job_id),
                "error_code": exc.__class__.__name__,
            },
        )
        return False
    WorkPlan.objects.filter(pk=plan.pk, status=WorkPlan.Status.READY).update(
        status=WorkPlan.Status.QUEUED,
        last_queued_at=timezone.now(),
    )
    InternalOpportunity.objects.filter(pk=opportunity.pk).update(
        state=InternalOpportunity.State.QUEUED,
        reason_codes=[],
    )
    return True


@transaction.atomic
def mark_internal_execution_started(*, job: Job) -> None:
    """Expose truthful factory execution state without affecting revenue truth."""
    InternalOpportunity.objects.filter(
        job=job,
        state__in=[InternalOpportunity.State.ECONOMICS_APPROVED, InternalOpportunity.State.QUEUED],
    ).update(state=InternalOpportunity.State.EXECUTING)


@transaction.atomic
def record_internal_execution_outcome(*, execution, qa_passed: bool) -> None:
    """Convert independently QA'd owned work into bounded unpublished inventory."""
    opportunity = (
        InternalOpportunity.objects.select_for_update()
        .select_related("product")
        .filter(job=execution.job)
        .first()
    )
    if opportunity is None:
        return
    product = ProductCandidate.objects.select_for_update().get(pk=opportunity.product_id)
    calls = GenXCall.objects.filter(
        job=execution.job,
        created_at__gte=execution.started_at,
        created_at__lte=execution.ended_at or timezone.now(),
    )
    actual_cost = calls.aggregate(total=Sum("cost_equivalent"))["total"] or Decimal("0")
    product.cost_basis += actual_cost
    evidence = dict(product.qa_evidence or {})
    evidence["last_execution_id"] = execution.id
    evidence["last_qa_passed"] = bool(qa_passed)
    evidence["last_checked_at"] = timezone.now().isoformat()
    if qa_passed:
        product.inventory_quantity = min(product.inventory_quantity + 1, product.max_inventory_quantity)
        product.state = ProductCandidate.State.READY_TO_PUBLISH
        opportunity.state = InternalOpportunity.State.COMPLETED
        opportunity.reason_codes = []
    else:
        evidence["rejected_assets"] = int(evidence.get("rejected_assets") or 0) + 1
        product.state = ProductCandidate.State.ECONOMICS_APPROVED
        opportunity.state = InternalOpportunity.State.BLOCKED
        opportunity.reason_codes = ["INDEPENDENT_QA_REJECTED"]
    product.qa_evidence = evidence
    _refresh_product_roi(product)
    product.save(
        update_fields=[
            "cost_basis",
            "inventory_quantity",
            "state",
            "qa_evidence",
            "net_profit",
            "return_on_production_cost",
            "break_even_at",
            "updated_at",
        ]
    )
    opportunity.save(update_fields=["state", "reason_codes", "updated_at"])
    AuditEvent.objects.create(
        event_type="product_factory.qa_accepted" if qa_passed else "product_factory.qa_rejected",
        actor="qa-runtime",
        metadata={
            "product": product.slug,
            "job_id": str(execution.job_id),
            "execution_id": execution.id,
            "actual_cost": str(actual_cost),
            "auto_publish": False,
            "revenue_recognized": False,
        },
    )


def product_factory_cycle() -> dict[str, Any]:
    sync = sync_capability_monetization_matrix()
    candidates = generate_product_candidates()
    replenishment_candidates = generate_internal_opportunities()
    paused = apply_stop_loss()
    policy = load_policy()
    admitted = None
    if policy["enabled"] and current_mode() in {AutonomyMode.LOW_RISK, AutonomyMode.FULL}:
        admitted = (
            InternalOpportunity.objects.select_related("product", "job")
            .filter(state=InternalOpportunity.State.ECONOMICS_APPROVED, job__isnull=False)
            .order_by("created_at")
            .first()
        ) or admit_next_internal_opportunity()
    queued = bool(admitted and _queue_internal_opportunity(admitted))
    return {
        "policy": policy,
        "capability_matrix": sync,
        "candidates": candidates,
        "replenishment_candidates": replenishment_candidates,
        "stop_loss_paused": paused,
        "admitted_opportunity_id": admitted.id if admitted else None,
        "paid_execution_started": queued,
        "auto_publication": False,
    }


def factory_snapshot() -> dict[str, Any]:
    policy = load_policy()
    products = ProductCandidate.objects.all().order_by("product_class", "slug")
    return {
        "policy": policy,
        "daily_internal_credits": str(_daily_internal_credits()),
        "products": [
            {
                "slug": row.slug,
                "product_class": row.product_class,
                "state": row.state,
                "target_buyer": row.target_buyer,
                "channels": row.intended_channels,
                "expected_net": str(row.expected_net),
                "currency": row.currency,
                "expected_margin": str(row.expected_margin),
                "confidence": str(row.confidence),
                "inventory": row.inventory_quantity,
                "sales": row.sales,
                "gross_revenue": str(row.gross_revenue),
                "payout_received": str(row.payout_received),
                "net_profit": str(row.net_profit),
                "return_on_production_cost": str(row.return_on_production_cost) if row.return_on_production_cost is not None else None,
                "published_at": row.published_at.isoformat() if row.published_at else None,
                "first_sale_at": row.first_sale_at.isoformat() if row.first_sale_at else None,
                "break_even_at": row.break_even_at.isoformat() if row.break_even_at else None,
                "paused_reason": row.paused_reason,
            }
            for row in products
        ],
        "capability_matrix_count": CapabilityMonetization.objects.count(),
        "ready_to_publish": products.filter(state=ProductCandidate.State.READY_TO_PUBLISH).count(),
        "truth": "Generated or published inventory is not revenue; only authoritative sales and owner receipt evidence update commercial truth.",
    }
