from __future__ import annotations

import hashlib
import hmac
import os
from decimal import Decimal, DecimalException

from django.db import transaction
from django.db.models import Count, Max, Q, Sum

from control.models import (
    BuyerProfile,
    CapabilityEvaluation,
    CommercialAPIProduct,
    CommercialAPIUsage,
    CommercialProductPackage,
    ConversionEvent,
    InboundOrder,
    OfferEvent,
    OfferExperiment,
    OfferVariant,
    OpportunityDecision,
    ProductCandidate,
    ServiceOffering,
)

ZERO = Decimal(0)
ALLOWED_FUNNEL_EVENTS = {
    "PRODUCT_IMPRESSION", "PRODUCT_VIEW", "API_DOCUMENTATION_VIEW", "PRICING_VIEW",
    "CTA_CLICK", "ENQUIRY_START", "ENQUIRY_COMPLETED",
}
SETTLED_EVENT_TYPES = {"SETTLED", "REFUND", "REVERSAL"}


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (DecimalException, TypeError, ValueError):
        return ZERO


def _privacy_hash(value: str) -> str:
    pepper = os.getenv("CUSTOMER_REFERENCE_PEPPER", os.getenv("AUTH_THROTTLE_PEPPER", "development-customer-reference-pepper"))
    return hmac.new(pepper.encode(), value.encode(), hashlib.sha256).hexdigest()


@transaction.atomic
def refresh_buyer_profile(*, channel: str, external_reference: str) -> BuyerProfile:
    reference_hash = _privacy_hash(f"{channel}:{external_reference}")
    profile, _ = BuyerProfile.objects.select_for_update().get_or_create(channel=channel[:80], external_reference_hash=reference_hash)
    orders = InboundOrder.objects.filter(marketplace__slug=channel, buyer_reference=external_reference)
    settled = orders.filter(settlement_events__state="SETTLED", settlement_events__authoritative=True).distinct()
    settlement_totals = settled.aggregate(
        gross=Sum("settlement_events__gross"), fees=Sum("settlement_events__fee"),
        last_order=Max("created_at"),
    )
    api_usage = CommercialAPIUsage.objects.filter(buyer=profile, authoritative_settlement=True).aggregate(
        gross=Sum("settled_revenue"), fees=Sum("marketplace_fee"), costs=Sum("execution_cost"), profit=Sum("settled_net_profit"), count=Count("id"),
    )
    order_gross = _decimal(settlement_totals["gross"])
    api_gross = _decimal(api_usage["gross"])
    fees = _decimal(settlement_totals["fees"]) + _decimal(api_usage["fees"])
    costs = _decimal(api_usage["costs"])
    profit = order_gross + api_gross - fees - costs
    order_count = orders.count() + int(api_usage["count"] or 0)
    settled_count = settled.count() + int(api_usage["count"] or 0)
    preferred = list(orders.exclude(job__normalized_payload__operation="").values_list("job__normalized_payload__operation", flat=True)[:10])
    profile.orders = order_count
    profile.completed_orders = orders.filter(status__in=[InboundOrder.Status.DELIVERED, InboundOrder.Status.PAYOUT_PENDING, InboundOrder.Status.SETTLED]).count() + int(api_usage["count"] or 0)
    profile.settled_orders = settled_count
    profile.settled_gross = order_gross + api_gross
    profile.marketplace_fees = fees
    profile.attributable_execution_costs = costs
    profile.settled_net_profit = profit
    profile.refund_count = orders.filter(status=InboundOrder.Status.REVERSED).count()
    profile.last_order_at = settlement_totals["last_order"]
    profile.preferred_operations = list(dict.fromkeys(str(item) for item in preferred if item))
    profile.repeat_buyer = order_count > 1
    profile.ltv_estimate = max(ZERO, profit)
    profile.sample_count = settled_count
    profile.ltv_confidence = min(Decimal(1), Decimal(settled_count) / Decimal(10))
    profile.save()
    return profile


def bounded_customer_value_contribution(*, immediate_profit: Decimal, customer: BuyerProfile | None, acquisition_budget: Decimal = ZERO, policy_enabled: bool = False) -> dict:
    immediate_profit = _decimal(immediate_profit)
    if immediate_profit < 0:
        return {"allowed": False, "contribution": ZERO, "reason_codes": ["IMMEDIATE_HARD_LOSS_FLOOR"]}
    if not policy_enabled or customer is None or customer.sample_count < 3 or customer.ltv_confidence < Decimal("0.50"):
        return {"allowed": True, "contribution": ZERO, "reason_codes": ["CUSTOMER_VALUE_INSUFFICIENT_EVIDENCE"]}
    cap = min(max(ZERO, _decimal(acquisition_budget)), immediate_profit * Decimal("0.10"))
    contribution = min(cap, max(ZERO, customer.ltv_estimate) * customer.ltv_confidence * Decimal("0.02"))
    return {"allowed": True, "contribution": contribution, "reason_codes": ["BOUNDED_CUSTOMER_VALUE_SECONDARY_FACTOR"]}


@transaction.atomic
def record_offer_event(*, variant: OfferVariant, event_id: str, event_type: str, anonymous_reference: str = "", order: InboundOrder | None = None, authoritative: bool = False, settled_gross: Decimal = ZERO, settled_cost: Decimal = ZERO) -> tuple[OfferEvent, bool]:
    event_type = event_type.upper()
    if event_type in SETTLED_EVENT_TYPES and not authoritative:
        raise ValueError("AUTHORITATIVE_SETTLEMENT_EVIDENCE_REQUIRED")
    if not event_id:
        raise ValueError("OFFER_EVENT_ID_REQUIRED")
    row, created = OfferEvent.objects.get_or_create(
        variant=variant, event_id=event_id[:180],
        defaults={
            "event_type": event_type[:40], "anonymous_reference_hash": _privacy_hash(anonymous_reference) if anonymous_reference else "",
            "order": order, "authoritative": authoritative, "settled_gross": settled_gross, "settled_cost": settled_cost,
        },
    )
    if not created and (row.event_type != event_type[:40] or row.authoritative != authoritative or row.settled_gross != settled_gross or row.settled_cost != settled_cost):
        raise ValueError("OFFER_EVENT_IDEMPOTENCY_CONFLICT")
    return row, created


def recommend_experiment_winner(experiment: OfferExperiment) -> dict:
    rows = []
    for variant in experiment.variants.filter(active=True):
        events = variant.events.all()
        exposures = events.filter(event_type__in=["IMPRESSION", "PRODUCT_VIEW", "API_DOC_VIEW"]).count()
        settled = events.filter(event_type="SETTLED", authoritative=True)
        aggregates = settled.aggregate(gross=Sum("settled_gross"), cost=Sum("settled_cost"), outcomes=Count("id"))
        profit = _decimal(aggregates["gross"]) - _decimal(aggregates["cost"])
        risk_adjusted = profit * min(Decimal(1), Decimal(aggregates["outcomes"] or 0) / Decimal(max(1, experiment.minimum_settled_outcomes)))
        rows.append({"variant": variant.slug, "variant_id": variant.id, "exposures": exposures, "settled_outcomes": aggregates["outcomes"] or 0, "settled_net_profit": profit, "risk_adjusted_profit": risk_adjusted, "profit_per_exposure": risk_adjusted / exposures if exposures else ZERO})
    eligible = [row for row in rows if row["exposures"] >= experiment.minimum_exposures and row["settled_outcomes"] >= experiment.minimum_settled_outcomes]
    winner = max(eligible, key=lambda row: row["profit_per_exposure"], default=None)
    return {"experiment": experiment.slug, "state": experiment.state, "winner": winner, "variants": rows, "classification": "PASS" if winner else "INSUFFICIENT_EVIDENCE", "note": "Clicks and views are diagnostics; only authoritative settled economics can select a winner."}


@transaction.atomic
def record_conversion_event(*, event_id: str, event_type: str, anonymous_reference: str, product_slug: str = "", variant_id=None, source: str = "", metadata: dict | None = None) -> tuple[ConversionEvent, bool]:
    event_type = event_type.upper()
    if event_type not in ALLOWED_FUNNEL_EVENTS:
        raise ValueError("CONVERSION_EVENT_TYPE_NOT_ALLOWED")
    if not event_id or not anonymous_reference:
        raise ValueError("CONVERSION_EVENT_IDENTITY_REQUIRED")
    product = CommercialAPIProduct.objects.filter(slug=product_slug).first() if product_slug else None
    variant = OfferVariant.objects.filter(pk=variant_id).first() if variant_id else None
    row, created = ConversionEvent.objects.get_or_create(
        event_id=event_id[:180],
        defaults={
            "event_type": event_type, "anonymous_reference_hash": _privacy_hash(anonymous_reference),
            "product": product, "variant": variant, "source": source[:120],
            "metadata": {key: value for key, value in (metadata or {}).items() if key not in {"email", "name", "phone", "ip", "user_agent"}},
        },
    )
    if not created and row.event_type != event_type:
        raise ValueError("CONVERSION_EVENT_IDEMPOTENCY_CONFLICT")
    return row, created


def evaluate_capability_candidate(*, operation: str, capability: str, candidate_version: str, baseline_version: str, fixture_key: str, quality_score: Decimal, baseline_quality_score: Decimal, monetary_cost: Decimal, baseline_monetary_cost: Decimal, latency_ms: int, baseline_latency_ms: int, repair_rate: Decimal = ZERO, completion_rate: Decimal = Decimal(1), evidence: dict | None = None) -> CapabilityEvaluation:
    quality_score, baseline_quality_score = _decimal(quality_score), _decimal(baseline_quality_score)
    monetary_cost, baseline_monetary_cost = _decimal(monetary_cost), _decimal(baseline_monetary_cost)
    if not evidence or completion_rate <= 0:
        decision = "INSUFFICIENT_EVIDENCE"
    elif quality_score < baseline_quality_score * Decimal("0.98") or repair_rate > Decimal("0.20") or completion_rate < Decimal("0.95") or monetary_cost > max(Decimal("0.000001"), baseline_monetary_cost) * Decimal("1.15") and quality_score <= baseline_quality_score * Decimal("1.05"):
        decision = "REGRESSION"
    elif quality_score > baseline_quality_score * Decimal("1.02") and monetary_cost <= max(Decimal("0.000001"), baseline_monetary_cost) * Decimal("1.10"):
        decision = "BETTER"
    else:
        decision = "EQUIVALENT"
    row, _ = CapabilityEvaluation.objects.update_or_create(
        operation=operation, candidate_version=candidate_version, fixture_key=fixture_key,
        defaults={
            "capability": capability, "baseline_version": baseline_version,
            "quality_score": quality_score, "baseline_quality_score": baseline_quality_score,
            "latency_ms": max(0, latency_ms), "baseline_latency_ms": max(0, baseline_latency_ms),
            "monetary_cost": monetary_cost, "baseline_monetary_cost": baseline_monetary_cost,
            "repair_rate": repair_rate, "completion_rate": completion_rate,
            "decision": decision, "evidence": evidence or {},
        },
    )
    return row


PACKAGE_DEFINITIONS = (
    ("website-brand-package", "AUTOMATION", "Website to brand package", "Brand and marketing teams", "Turn an authorized website into a reusable brand evidence package.", ["website_research", "content_package"], ["GenX credential and live catalogue", "authorized public website"]),
    ("website-marketing-campaign", "AUTOMATION", "Website to marketing campaign", "Small business marketing teams", "Build a grounded campaign package from supplied website evidence.", ["website_research", "marketing_campaign"], ["GenX credential and live catalogue", "owner publication approval"]),
    ("research-report", "SERVICE", "Research to report", "Strategy and operations teams", "Produce a cited research report with independent QA.", ["research_report", "document_production"], ["GenX credential and live catalogue"]),
    ("research-data-deck", "AUTOMATION", "Research to spreadsheet and presentation", "Analysts and decision makers", "Transform grounded research into structured analysis and an executive deck.", ["research_report", "spreadsheet_report", "presentation_production"], ["GenX credential and live catalogue"]),
    ("document-intelligence", "API", "Document intelligence package", "Document-heavy operations", "Extract, OCR, classify and structure bounded business documents.", ["document_extract_text", "ocr_document", "extract_structured_facts"], ["API credential issuance", "GenX credential for semantic extraction"]),
    ("structured-data-cleanup", "API", "Structured data cleanup package", "Developers and analysts", "Normalize, deduplicate, convert and validate recurring tabular feeds.", ["tabular_normalize", "tabular_deduplicate", "tabular_convert", "tabular_schema_validate"], ["API credential issuance"]),
)


@transaction.atomic
def bootstrap_commercial_packages() -> dict[str, int]:
    created = updated = 0
    for slug, package_type, title, target, problem, operations, blockers in PACKAGE_DEFINITIONS:
        offering = ServiceOffering.objects.filter(operation=operations[0]).order_by("slug").first()
        _row, was_created = CommercialProductPackage.objects.update_or_create(
            slug=slug,
            defaults={
                "package_type": package_type, "offering": offering, "title": title, "target_buyer": target,
                "problem": problem, "promised_output": title,
                "required_inputs": {"bounded": True, "customer_supplied_or_authorized": True},
                "operations": operations, "setup_steps": ["validate inputs", "run canonical workflow", "independent QA", "owner-approved publication"],
                "credentials_needed": blockers, "demo_scenario": {"fixture_only": True},
                "sample_output": {"state": "FIXTURE_AVAILABLE_AFTER_QA"},
                "usage_instructions": "Supply only authorized inputs. Outputs are released after canonical QA.",
                "limitations": ["No external publication while autonomy is OFF", "No settlement claim without authoritative evidence"],
                "qa_evidence": {"required": True, "source": "canonical execution lifecycle"},
                "pricing_model": {"source": "Profit Brain", "minimum_margin_required": True},
                "channel_candidates": ["direct", "rapidapi"] if package_type == "API" else ["direct", "apify", "service_marketplaces"],
                "support_information": {"route": "owner-gated"},
                "listing_copy": {"headline": title, "description": problem},
                "asset_requirements": ["sample output", "quality evidence", "limitations"],
                "publication_blockers": blockers,
                "launch_rank": "LAUNCH_FIRST" if slug == "structured-data-cleanup" else "PROVE_FIRST",
            },
        )
        created += int(was_created); updated += int(not was_created)
    return {"created": created, "updated": updated, "total": len(PACKAGE_DEFINITIONS)}


def launch_inventory() -> list[dict]:
    rows = []
    for package in CommercialProductPackage.objects.order_by("slug"):
        blockers = list(package.publication_blockers or [])
        cost = ZERO
        margin = ZERO
        if package.offering_id:
            cost = package.offering.expected_genx_cost + package.offering.expected_external_cost + package.offering.expected_operational_cost
            price = package.offering.advertised_price
            margin = (price - cost) / price if price else ZERO
        if not blockers and margin >= Decimal("0.20"):
            classification = "LAUNCH_FIRST"
        elif blockers and any("CREDENTIAL" in item.upper() or "ACCOUNT" in item.upper() for item in blockers):
            classification = "PROVE_FIRST"
        elif blockers:
            classification = "BLOCKED"
        else:
            classification = "LAUNCH_NEXT"
        if package.slug == "structured-data-cleanup":
            classification = "LAUNCH_FIRST"
        rows.append({
            "slug": package.slug, "title": package.title, "type": package.package_type,
            "classification": classification, "expected_cost": str(cost), "expected_margin": str(margin),
            "recurring_revenue_potential": package.package_type in {"API", "AUTOMATION"},
            "repeatability": "HIGH", "demand_evidence": "NOT_YET_PROVEN", "support_burden": "BOUNDED",
            "external_blockers": blockers,
        })
    order = {"LAUNCH_FIRST": 0, "LAUNCH_NEXT": 1, "PROVE_FIRST": 2, "BLOCKED": 3}
    return sorted(rows, key=lambda row: (order[row["classification"]], row["expected_cost"], row["slug"]))


def profit_explanation_rows(limit: int = 50) -> list[dict]:
    rows = []
    decisions = OpportunityDecision.objects.select_related("job", "job__marketplace", "job__jobscore", "capacity", "pricing_strategy").order_by("-created_at")[:limit]
    for decision in decisions:
        score = decision.job.jobscore
        detail = decision.details if isinstance(decision.details, dict) else {}
        rows.append({
            "opportunity": decision.job.title, "job_id": str(decision.job_id), "marketplace": decision.job.marketplace.display_name,
            "expected_gross": str(score.recommended_offer or decision.job.reward),
            "marketplace_fee": str((score.recommended_offer or decision.job.reward) * decision.job.marketplace.fee_rate),
            "expected_execution_cost": str(score.expected_genx_cost), "other_variable_cost": str(score.expected_external_cost),
            "expected_net_profit": str(decision.expected_cash_profit), "risk_adjusted_profit": str(decision.risk_adjusted_profit),
            "profit_per_minute": str(score.expected_profit_per_minute), "payout_probability": str(score.p_payment),
            "acceptance_probability": str(score.p_accept), "customer_value_contribution": str(detail.get("customer_value_contribution", "0")),
            "reputation_contribution": str(decision.reputation_contribution), "learning_contribution": str(decision.learning_contribution),
            "concentration_penalty": str(detail.get("concentration_penalty", "0")), "payment_risk_penalty": str(detail.get("payment_risk_penalty", "0")),
            "capacity_state": decision.utilization_state, "alternative_opportunity": detail.get("alternative_opportunity"),
            "selection_rank": detail.get("selection_rank"), "final_decision": "SELECT" if decision.allowed else "REJECT",
            "reason_codes": decision.reason_codes, "would_select_if_enabled": bool(not decision.allowed and decision.reason_codes and all("AUTONOM" in str(code) for code in decision.reason_codes)),
        })
    return rows


def commercial_snapshot() -> dict:
    products = CommercialAPIProduct.objects.prefetch_related("plans").order_by("slug")
    usages = CommercialAPIUsage.objects.aggregate(
        calls=Count("id"), estimated_cost=Sum("execution_cost"), settled=Sum("settled_revenue"), profit=Sum("settled_net_profit"), overage=Sum("gross_billed"),
    )
    customers = BuyerProfile.objects.aggregate(total=Count("id"), repeat=Count("id", filter=Q(repeat_buyer=True)), settled_profit=Sum("settled_net_profit"))
    experiments = [recommend_experiment_winner(row) for row in OfferExperiment.objects.prefetch_related("variants__events").order_by("slug")]
    return {
        "section": "commercial",
        "api_business": {
            "products": [{
                "slug": product.slug, "name": product.display_name, "proof_state": product.proof_state,
                "publication_state": product.publication_state, "plans": product.plans.filter(active=True).count(),
                "calls": product.usage.count(), "expected_cost": str(product.expected_execution_cost),
                "actual_cost": str(product.usage.aggregate(value=Sum("execution_cost"))["value"] or ZERO),
                "settled_revenue": str(product.usage.aggregate(value=Sum("settled_revenue"))["value"] or ZERO),
                "settled_net_profit": str(product.usage.aggregate(value=Sum("settled_net_profit"))["value"] or ZERO),
                "required_external_actions": product.required_external_actions,
            } for product in products],
            "calls": usages["calls"] or 0, "estimated_actual_execution_cost": str(usages["estimated_cost"] or ZERO),
            "settled_revenue": str(usages["settled"] or ZERO), "settled_net_profit": str(usages["profit"] or ZERO),
            "mrr": "0", "mrr_truth": "NO_AUTHORITATIVE_ACTIVE_PAID_SUBSCRIPTIONS",
            "overage_revenue": "0", "overage_truth": "NO_AUTHORITATIVE_SETTLED_OVERAGE",
        },
        "product_factory": {
            "candidates": ProductCandidate.objects.count(), "ready": ProductCandidate.objects.filter(state=ProductCandidate.State.READY_TO_PUBLISH).count(),
            "published": ProductCandidate.objects.filter(state=ProductCandidate.State.PUBLISHED).count(),
            "sales": ProductCandidate.objects.aggregate(value=Sum("sales"))["value"] or 0,
            "inventory": ProductCandidate.objects.aggregate(value=Sum("inventory_quantity"))["value"] or 0,
            "settled_profit": str(ProductCandidate.objects.aggregate(value=Sum("net_profit"))["value"] or ZERO),
        },
        "experiments": experiments,
        "customers": {"total": customers["total"] or 0, "repeat": customers["repeat"] or 0, "settled_profit": str(customers["settled_profit"] or ZERO), "retention": "NOT_YET_PROVEN" if not customers["total"] else "EVIDENCE_AVAILABLE"},
        "launch_inventory": launch_inventory(),
        "profit_explanations": profit_explanation_rows(),
    }
