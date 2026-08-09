from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from control.acquisition import paid_cost_envelope
from control.economics import EconomicsInput
from control.models import (
    AcquisitionPreflight,
    AuditEvent,
    GenXAccountSnapshot,
    InboundOrder,
    InboundSettlementEvent,
    Job,
    MarketServiceListing,
    Marketplace,
    Payout,
    PerformanceAggregate,
    QAResult,
    ServiceOffering,
)
from control.services.jobs import score_and_persist, transition_job
from control.services.profit_brain import (
    GrowthStage,
    UtilizationState,
    capture_capacity,
    evaluate_opportunity,
    persist_opportunity_decision,
    recommend_price,
)
from workers.registry import WorkerRegistryError, operation_spec


CENT = Decimal("0.01")
ZERO = Decimal("0")

SERVICE_CANDIDATE_OPERATIONS = (
    "research_report",
    "data_analysis_report",
    "spreadsheet_report",
    "json_to_csv",
    "csv_normalize",
    "tabular_convert",
    "tabular_normalize",
    "content_package",
    "technical_documentation",
    "presentation_create",
    "docx_create",
    "pdf_create",
    "seo_content_audit",
    "image_resize",
    "image_convert",
    "static_html_create",
    "public_web_extract",
    "code_change_small",
    "code_change_heavy",
)


def _decimal(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _service_slug(operation: str) -> str:
    return operation.replace("_", "-")[:50]


def sync_candidate_service_offerings() -> dict[str, int]:
    """Map only registered, independently-QA-profiled operations into disabled candidates."""
    created = updated = 0
    for operation in SERVICE_CANDIDATE_OPERATIONS:
        try:
            spec = operation_spec(operation)
        except WorkerRegistryError:
            continue
        if not spec.qa_profile:
            continue
        expected_genx = Decimal("0.25") if spec.requires_genx else ZERO
        expected_operational = Decimal("0.10")
        recommendation = recommend_price(
            total_expected_cost=expected_genx + expected_operational,
            advertised_budget=None,
            competitive_price=None,
            fee_rate=ZERO,
            utilization_state=UtilizationState.PARTIALLY_IDLE,
            growth_stage=GrowthStage.BOOTSTRAP,
        )
        _, was_created = ServiceOffering.objects.get_or_create(
            slug=_service_slug(operation),
            defaults={
                "display_name": operation.replace("_", " ").title(),
                "description": spec.description,
                "capability": spec.worker_class,
                "operation": operation,
                "worker_class": spec.worker_class,
                "pricing_model": ServiceOffering.PricingModel.FIXED_PROJECT,
                "advertised_price": recommendation.offered_price,
                "minimum_profitable_price": recommendation.minimum_profitable_price,
                "expected_genx_cost": expected_genx,
                "max_genx_credits": expected_genx,
                "expected_operational_cost": expected_operational,
                "expected_minutes": 60,
                "input_schema": {"type": "object", "additionalProperties": False},
                "output_schema": {"type": "object", "required": ["artifacts"]},
                "terms_metadata": {"qa_profile": spec.qa_profile, "auto_publish": False},
                "proof_evidence": {"registry_version": spec.version, "qa_profile": spec.qa_profile},
                "enabled": False,
                "accepting_orders": False,
                "proof_state": ServiceOffering.ProofState.SOURCE_PROVEN,
            },
        )
        created += int(was_created)
        updated += int(not was_created)
    return {"created": created, "updated": updated, "total": len(SERVICE_CANDIDATE_OPERATIONS)}


def service_capability_blockers(offering: ServiceOffering) -> list[str]:
    reasons: list[str] = []
    try:
        spec = operation_spec(offering.operation)
    except WorkerRegistryError:
        return ["WORKER_OPERATION_NOT_REGISTERED"]
    if spec.worker_class != offering.worker_class:
        reasons.append("WORKER_OPERATION_MISMATCH")
    if not spec.qa_profile:
        reasons.append("QA_PROFILE_NOT_REGISTERED")
    try:
        spec.build()
    except Exception:
        reasons.append("WORKER_RUNTIME_UNAVAILABLE")
    if offering.worker_class in {"code_small", "code_heavy", "ci_testing"} and os.getenv("SANDBOX_CODING_ENABLED", "0") != "1":
        reasons.append("CODING_SERVICE_BLOCKED_SANDBOX_OFF")
    if offering.worker_class == "public_web_data" and os.getenv("PUBLIC_WEB_DATA_ENABLED", "0") != "1":
        reasons.append("PUBLIC_WEB_SERVICE_BLOCKED_WEB_DISABLED")
    proof_id = offering.proof_evidence.get("qa_result_id") if isinstance(offering.proof_evidence, dict) else None
    proof_exists = bool(proof_id and QAResult.objects.filter(
        id=proof_id,
        passed=True,
        execution__status__in=["COMPLETED", "SUCCEEDED"],
        execution__result__operation=offering.operation,
        execution__worker__worker_class=offering.worker_class,
    ).exists())
    if offering.proof_state != ServiceOffering.ProofState.SELLABLE or not proof_exists:
        reasons.append("SERVICE_EXECUTION_NOT_PROVEN")
    if not offering.enabled:
        reasons.append("SERVICE_OFFERING_DISABLED")
    if not offering.accepting_orders:
        reasons.append("SERVICE_NOT_ACCEPTING_ORDERS")
    return list(dict.fromkeys(reasons))


@transaction.atomic
def refresh_service_offering_proof(offering: ServiceOffering) -> ServiceOffering:
    """Promote only from a real completed execution with passing QA for this operation."""
    try:
        spec = operation_spec(offering.operation)
        spec.build()
    except Exception:
        offering.proof_state = ServiceOffering.ProofState.UNPROVEN
        offering.save(update_fields=["proof_state", "updated_at"])
        return offering
    proof = QAResult.objects.filter(
        passed=True,
        execution__status__in=["COMPLETED", "SUCCEEDED"],
        execution__result__operation=offering.operation,
        execution__worker__worker_class=offering.worker_class,
    ).select_related("execution", "job").order_by("-created_at").first()
    if not proof:
        offering.proof_state = ServiceOffering.ProofState.SOURCE_PROVEN
    else:
        offering.proof_state = ServiceOffering.ProofState.SELLABLE
        offering.proof_evidence = {
            **offering.proof_evidence,
            "job_id": str(proof.job_id),
            "execution_id": proof.execution_id,
            "qa_result_id": proof.id,
            "qa_profile": spec.qa_profile,
            "proved_at": proof.created_at.isoformat(),
        }
    offering.save(update_fields=["proof_state", "proof_evidence", "updated_at"])
    return offering


@dataclass(frozen=True)
class OfferingPriceDecision:
    minimum_profitable_price: Decimal
    price: Decimal
    desired_margin: Decimal
    adjustment_fraction: Decimal
    reason_codes: tuple[str, ...]


def recommend_offering_price(
    offering: ServiceOffering,
    *,
    utilization_state: UtilizationState,
    competitive_price: Decimal | None = None,
    historical_win_rate: Decimal | None = None,
    market_fee_rate: Decimal | None = None,
) -> OfferingPriceDecision:
    performance = PerformanceAggregate.objects.filter(
        dimension_type="OPERATION", dimension_key=offering.operation,
    ).order_by("-window_end", "-created_at").first()
    revision_rate = _decimal(performance.revision_rate) if performance else ZERO
    qa_rate = _decimal(performance.qa_first_pass_rate) if performance else ZERO
    acceptance_rate = _decimal(performance.acceptance_rate) if performance else ZERO
    observed_win_rate = historical_win_rate
    if observed_win_rate is None and performance and performance.sample_count:
        observed_win_rate = acceptance_rate
    operational_cost = _decimal(offering.expected_operational_cost) * (Decimal("1") + min(Decimal("1"), max(ZERO, revision_rate)))
    total_cost = _decimal(offering.expected_genx_cost) + _decimal(offering.expected_external_cost) + operational_cost
    effective_fee_rate = _decimal(offering.platform_fee_rate) if market_fee_rate is None else _decimal(market_fee_rate)
    result = recommend_price(
        total_expected_cost=total_cost,
        advertised_budget=None,
        competitive_price=competitive_price or _decimal(offering.advertised_price) or None,
        fee_rate=effective_fee_rate,
        utilization_state=utilization_state,
        growth_stage=GrowthStage.PROFIT,
        historical_win_rate=observed_win_rate,
    )
    minimum = max(_decimal(offering.minimum_profitable_price), result.minimum_profitable_price)
    price = max(minimum, result.offered_price)
    decision = OfferingPriceDecision(minimum, _money(price), result.desired_margin, result.adjustment_fraction, result.reason_codes)
    AuditEvent.objects.create(
        event_type="service.pricing_recommended",
        actor="profit-brain",
        metadata={
            "offering_id": str(offering.id), "operation": offering.operation,
            "utilization_state": utilization_state.value, "decision": {key: str(value) for key, value in asdict(decision).items()},
            "inputs": {
                "market_fee_rate": str(effective_fee_rate),
                "expected_genx_cost": str(offering.expected_genx_cost),
                "max_genx_credits": str(offering.max_genx_credits),
                "expected_external_cost": str(offering.expected_external_cost),
                "expected_operational_cost_with_revision": str(operational_cost),
                "expected_minutes": offering.expected_minutes,
                "historical_win_or_acceptance_rate": None if observed_win_rate is None else str(observed_win_rate),
                "revision_rate": str(revision_rate),
                "qa_first_pass_rate": str(qa_rate),
                "competitive_price": None if competitive_price is None else str(competitive_price),
                "competition_evidence_available": competitive_price is not None,
            },
        },
    )
    return decision


def recommend_listing_price(
    listing: MarketServiceListing,
    *,
    utilization_state: UtilizationState,
    competitive_price: Decimal | None = None,
    historical_win_rate: Decimal | None = None,
) -> OfferingPriceDecision:
    """Apply the shared Profit Brain with the listing market's fee exactly once."""
    return recommend_offering_price(
        listing.offering,
        utilization_state=utilization_state,
        competitive_price=competitive_price,
        historical_win_rate=historical_win_rate,
        market_fee_rate=listing.marketplace.fee_rate,
    )


def listing_blockers(listing: MarketServiceListing, *, now=None) -> list[str]:
    now = now or timezone.now()
    offering = listing.offering
    market = listing.marketplace
    reasons = service_capability_blockers(offering)
    try:
        profile = market.integration_profile
    except Exception:
        profile = None
    policy = market.policy_versions.order_by("-checked_at", "-created_at").first()
    if not market.enabled:
        reasons.append("MARKET_DISABLED")
    if not market.payout_ready:
        reasons.append("PAYOUT_NOT_READY")
    if not market.south_africa_verified:
        reasons.append("SOUTH_AFRICA_PAYOUT_NOT_VERIFIED")
    if profile is None:
        reasons.append("MARKET_SELLER_PROFILE_MISSING")
    else:
        if not profile.seller_capabilities.get("publish_service"):
            reasons.append("PUBLISH_SERVICE_CAPABILITY_NOT_VERIFIED")
        if profile.hosting_policy == "OFFHOST_SETTLEMENT_REQUIRED":
            reasons.append("OFFHOST_SETTLEMENT_REQUIRED")
        elif profile.hosting_policy != "WEBDOCK_SAFE":
            reasons.append("WEBDOCK_HOSTING_POLICY_UNVERIFIED")
        reasons.extend(str(reason) for reason in profile.blockers)
    max_age = max(1, int(os.getenv("MARKET_POLICY_MAX_AGE_DAYS", "30")))
    if policy is None or not policy.automation_allowed:
        reasons.append("MARKET_MUTATION_POLICY_NOT_VERIFIED")
    elif policy.checked_at < now - timedelta(days=max_age):
        reasons.append("MARKET_POLICY_STALE")
    if policy and not policy.webdock_compatible:
        reasons.append("MARKET_RUNTIME_NOT_COMPATIBLE")
    credential_by_market = {
        "nevermined": "NEVERMINED_API_KEY",
        "skyfire": "SKYFIRE_SELLER_API_KEY",
        "hyrve": "HYRVE_API_KEY",
    }
    env_name = credential_by_market.get(market.slug)
    if env_name and not os.getenv(env_name, "").strip():
        reasons.append("ACCOUNT_NOT_CONFIGURED")
    if os.getenv("SERVICE_AUTO_PUBLISH_ENABLED", "0") != "1":
        reasons.append("SERVICE_AUTO_PUBLISH_DISABLED")
    if market.slug == "nevermined":
        price_type = str(listing.platform_metadata.get("price_type") or "").upper()
        if listing.currency != "USD" or price_type != "FIXED_FIAT_PRICE":
            reasons.append("NEVERMINED_WEBDOCK_FIAT_ONLY")
        if os.getenv("NEVERMINED_AUTO_PUBLISH_ENABLED", "0") != "1":
            reasons.append("NEVERMINED_AUTO_PUBLISH_DISABLED")
    if market.slug == "skyfire":
        settlement_type = str(listing.platform_metadata.get("settlement_type") or "").upper()
        if settlement_type not in {"CARD", "BANK"}:
            reasons.append("SKYFIRE_NON_CRYPTO_SETTLEMENT_REQUIRED")
        if os.getenv("SKYFIRE_AUTO_PUBLISH_ENABLED", "0") != "1":
            reasons.append("SKYFIRE_AUTO_PUBLISH_DISABLED")
    if market.slug == "hyrve":
        reasons.append("PUBLIC_API_CONTRACT_NOT_VERIFIED")
    return list(dict.fromkeys(reasons))


@transaction.atomic
def refresh_listing_truth(listing: MarketServiceListing) -> MarketServiceListing:
    reasons = listing_blockers(listing)
    listing.status = MarketServiceListing.Status.BLOCKED if reasons else MarketServiceListing.Status.READY
    listing.failure_code = reasons[0] if reasons else ""
    listing.failure_detail = ",".join(reasons)
    policy = listing.marketplace.policy_versions.order_by("-checked_at", "-created_at").first()
    listing.policy_hash = policy.policy_hash if policy else ""
    listing.save(update_fields=["status", "failure_code", "failure_detail", "policy_hash", "updated_at"])
    AuditEvent.objects.create(
        event_type="service.listing_truth_refreshed",
        actor="seller-portfolio",
        metadata={"listing_id": str(listing.id), "status": listing.status, "reason_codes": reasons},
    )
    return listing


@transaction.atomic
def pause_service_listing(listing: MarketServiceListing, *, reason: str, actor: str = "owner") -> MarketServiceListing:
    if not reason.strip():
        raise ValueError("SERVICE_LISTING_PAUSE_REASON_REQUIRED")
    listing = MarketServiceListing.objects.select_for_update().get(pk=listing.pk)
    listing.status = MarketServiceListing.Status.PAUSED
    listing.failure_code = "OWNER_OR_POLICY_PAUSED"
    listing.failure_detail = reason[:2000]
    listing.save(update_fields=["status", "failure_code", "failure_detail", "updated_at"])
    AuditEvent.objects.create(
        event_type="service.listing_paused", actor=actor,
        metadata={"listing_id": str(listing.id), "reason": reason[:500]},
    )
    return listing


@transaction.atomic
def record_listing_publication(
    listing: MarketServiceListing,
    *,
    remote_listing_id: str,
    remote_reference: str,
    remote_version: str,
    authoritative_evidence: dict,
) -> MarketServiceListing:
    listing = MarketServiceListing.objects.select_for_update().get(pk=listing.pk)
    if listing.status != MarketServiceListing.Status.READY:
        raise ValueError("SERVICE_LISTING_NOT_READY_FOR_PUBLICATION_RECORD")
    if not remote_listing_id.strip() or not remote_reference.strip() or not authoritative_evidence:
        raise ValueError("SERVICE_LISTING_PUBLICATION_EVIDENCE_REQUIRED")
    listing.remote_listing_id = remote_listing_id[:255]
    listing.remote_reference = remote_reference[:700]
    listing.remote_version = remote_version[:80]
    listing.platform_metadata = {**listing.platform_metadata, "publication_evidence": authoritative_evidence}
    listing.status = MarketServiceListing.Status.PUBLISHED
    listing.published_at = timezone.now()
    listing.last_synced_at = timezone.now()
    listing.failure_code = ""
    listing.failure_detail = ""
    listing.save()
    AuditEvent.objects.create(
        event_type="service.listing_publication_reconciled", actor=f"market:{listing.marketplace.slug}",
        metadata={"listing_id": str(listing.id), "remote_listing_id": listing.remote_listing_id},
    )
    return listing


@transaction.atomic
def version_service_offering(offering: ServiceOffering, *, changes: dict, actor: str = "owner") -> ServiceOffering:
    allowed = {"display_name", "description", "advertised_price", "sla_minutes", "input_schema", "output_schema", "terms_metadata"}
    unknown = set(changes) - allowed
    if unknown or not changes:
        raise ValueError("SERVICE_VERSION_CHANGESET_INVALID")
    offering = ServiceOffering.objects.select_for_update().get(pk=offering.pk)
    for field, value in changes.items():
        setattr(offering, field, value)
    offering.version += 1
    offering.save(update_fields=[*changes.keys(), "version", "updated_at"])
    AuditEvent.objects.create(
        event_type="service.offering_versioned", actor=actor,
        metadata={"offering_id": str(offering.id), "version": offering.version, "changed_fields": sorted(changes)},
    )
    return offering


def _validate_order_payload(payload: dict) -> tuple[dict, list]:
    if not isinstance(payload, dict):
        raise ValueError("INBOUND_PAYLOAD_INVALID")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > max(1024, int(os.getenv("INBOUND_SERVICE_MAX_REQUEST_BYTES", "262144"))):
        raise ValueError("INBOUND_REQUEST_TOO_LARGE")
    requirements = payload.get("requirements") or {}
    assets = payload.get("input_assets") or []
    if not isinstance(requirements, dict) or not isinstance(assets, list):
        raise ValueError("INBOUND_REQUIREMENTS_INVALID")
    if len(assets) > max(0, int(os.getenv("INBOUND_SERVICE_MAX_ASSETS", "12"))):
        raise ValueError("INBOUND_ASSET_COUNT_EXCEEDED")
    max_asset = max(0, int(os.getenv("INBOUND_SERVICE_MAX_ASSET_BYTES", "104857600")))
    for asset in assets:
        if not isinstance(asset, dict) or not str(asset.get("asset_id") or "") or not str(asset.get("sha256") or ""):
            raise ValueError("INBOUND_ASSET_REFERENCE_INVALID")
        if any(key in asset for key in ("path", "local_path", "file_content", "data")):
            raise ValueError("INBOUND_UNSAFE_FILE_MATERIAL_REJECTED")
        if int(asset.get("size_bytes") or 0) < 0 or int(asset.get("size_bytes") or 0) > max_asset:
            raise ValueError("INBOUND_ASSET_SIZE_EXCEEDED")
    return requirements, assets


def _inbound_payment_probability(funding_state: str) -> Decimal:
    return {
        "AUTHORIZED": Decimal("0.85"),
        "FUNDED": Decimal("0.95"),
        "ESCROW": Decimal("0.95"),
        "PAYOUT_PENDING": Decimal("0.98"),
    }.get(funding_state.upper(), Decimal("0.50"))


@transaction.atomic
def record_inbound_service_message(order: InboundOrder, *, remote_id: str, content: str, actor: str) -> bool:
    if not remote_id.strip() or not actor.strip() or not content.strip():
        raise ValueError("INBOUND_SERVICE_MESSAGE_INVALID")
    if len(content.encode()) > max(1024, int(os.getenv("INBOUND_SERVICE_MAX_MESSAGE_BYTES", "32768"))):
        raise ValueError("INBOUND_SERVICE_MESSAGE_TOO_LARGE")
    order = InboundOrder.objects.select_for_update().get(pk=order.pk)
    messages = list(order.messages or [])
    if any(str(message.get("remote_id") or "") == remote_id for message in messages if isinstance(message, dict)):
        return False
    messages.append({
        "remote_id": remote_id[:255], "content": content, "actor": actor[:120], "received_at": timezone.now().isoformat(),
    })
    order.messages = messages[-100:]
    order.save(update_fields=["messages", "updated_at"])
    AuditEvent.objects.create(
        event_type="inbound.service_message_recorded", actor=actor,
        metadata={"order_id": str(order.id), "remote_id": remote_id[:255]},
    )
    return True


@transaction.atomic
def record_inbound_usage(
    order: InboundOrder,
    *,
    remote_event_id: str,
    units: Decimal,
    unit_type: str,
    authoritative_evidence: dict,
) -> bool:
    units = _decimal(units)
    if not remote_event_id.strip() or not unit_type.strip() or units < 0 or not authoritative_evidence:
        raise ValueError("INBOUND_USAGE_EVIDENCE_INVALID")
    order = InboundOrder.objects.select_for_update().get(pk=order.pk)
    usage = dict(order.usage or {})
    events = list(usage.get("events") or [])
    if any(str(event.get("remote_event_id") or "") == remote_event_id for event in events if isinstance(event, dict)):
        return False
    events.append({
        "remote_event_id": remote_event_id[:255], "units": str(units), "unit_type": unit_type[:80],
        "evidence": authoritative_evidence, "observed_at": timezone.now().isoformat(),
    })
    usage["events"] = events[-1000:]
    usage["total_units"] = str(sum((_decimal(event.get("units")) for event in events), ZERO))
    order.usage = usage
    order.save(update_fields=["usage", "updated_at"])
    AuditEvent.objects.create(
        event_type="inbound.usage_metered", actor=f"market:{order.marketplace.slug}",
        metadata={"order_id": str(order.id), "remote_event_id": remote_event_id[:255], "units": str(units), "unit_type": unit_type[:80]},
    )
    return True


@transaction.atomic
def record_inbound_delivery(order: InboundOrder, *, remote_reference: str, actor: str) -> InboundOrder:
    order = InboundOrder.objects.select_for_update().select_related("job").get(pk=order.pk)
    if order.job.state != Job.State.SUBMITTED:
        raise ValueError("INBOUND_DELIVERY_REQUIRES_CANONICAL_SUBMISSION")
    if not remote_reference.strip() or not actor.strip():
        raise ValueError("INBOUND_DELIVERY_REFERENCE_REQUIRED")
    order.remote_state = "DELIVERED"
    order.status = InboundOrder.Status.DELIVERED
    order.settlement_reference = remote_reference[:255]
    order.save(update_fields=["remote_state", "status", "settlement_reference", "updated_at"])
    AuditEvent.objects.create(
        event_type="inbound.delivery_reconciled", actor=actor,
        metadata={"order_id": str(order.id), "job_id": str(order.job_id), "remote_reference": remote_reference[:255]},
    )
    return order


@transaction.atomic
def receive_inbound_order(
    *,
    marketplace: Marketplace,
    listing: MarketServiceListing,
    remote_order_id: str,
    idempotency_key: str,
    payload: dict,
    authenticated_market_identity: bool,
    authenticated_at,
) -> tuple[InboundOrder, bool]:
    if not authenticated_market_identity:
        raise ValueError("INBOUND_MARKET_AUTHENTICATION_REQUIRED")
    if not remote_order_id.strip() or not idempotency_key.strip():
        raise ValueError("INBOUND_IDEMPOTENCY_REQUIRED")
    now = timezone.now()
    if authenticated_at is None or abs((now - authenticated_at).total_seconds()) > max(30, int(os.getenv("INBOUND_SERVICE_REPLAY_WINDOW_SECONDS", "300"))):
        raise ValueError("INBOUND_REPLAY_WINDOW_EXCEEDED")
    if listing.marketplace_id != marketplace.id:
        raise ValueError("INBOUND_MARKET_LISTING_MISMATCH")
    requirements, assets = _validate_order_payload(payload)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    existing = InboundOrder.objects.select_for_update().filter(marketplace=marketplace, remote_order_id=remote_order_id).first()
    if existing is None:
        existing = InboundOrder.objects.select_for_update().filter(marketplace=marketplace, idempotency_key=idempotency_key).first()
    if existing:
        if existing.request_digest != digest:
            raise ValueError("INBOUND_IDEMPOTENCY_PAYLOAD_CONFLICT")
        return existing, False
    recent_limit = max(1, int(os.getenv("INBOUND_SERVICE_MAX_REQUESTS_PER_MINUTE", "60")))
    if InboundOrder.objects.filter(marketplace=marketplace, created_at__gte=now - timedelta(minutes=1)).count() >= recent_limit:
        raise ValueError("INBOUND_MARKET_RATE_LIMIT_EXCEEDED")
    quoted_price = _decimal(payload.get("quoted_price"))
    platform_fee = _decimal(payload.get("platform_fee"))
    if quoted_price <= 0 or platform_fee < 0 or platform_fee > quoted_price:
        raise ValueError("INBOUND_ECONOMIC_INPUT_INVALID")
    currency = str(payload.get("currency") or listing.currency).upper()
    if currency != listing.currency or currency != listing.offering.currency:
        raise ValueError("INBOUND_CURRENCY_MISMATCH")
    deadline = payload.get("deadline")
    if isinstance(deadline, str):
        deadline = parse_datetime(deadline)
    if payload.get("deadline") and deadline is None:
        raise ValueError("INBOUND_DEADLINE_INVALID")
    if deadline and timezone.is_naive(deadline):
        deadline = timezone.make_aware(deadline)
    if deadline and deadline <= now:
        raise ValueError("INBOUND_DEADLINE_EXPIRED")
    order = InboundOrder.objects.create(
        marketplace=marketplace,
        listing=listing,
        remote_order_id=remote_order_id[:255],
        idempotency_key=idempotency_key[:255],
        buyer_reference=str(payload.get("buyer_reference") or "")[:255],
        requirements=requirements,
        input_assets=assets,
        quoted_price=quoted_price,
        currency=currency,
        platform_fee=platform_fee,
        funding_state=str(payload.get("funding_state") or "UNVERIFIED")[:80],
        deadline=deadline,
        remote_state=str(payload.get("remote_state") or "")[:80],
        messages=payload.get("messages") if isinstance(payload.get("messages"), list) else [],
        request_digest=digest,
        authenticated_at=authenticated_at,
    )
    offering = listing.offering
    job = Job.objects.create(
        marketplace=marketplace,
        external_id=f"inbound:{remote_order_id}"[:255],
        title=f"Inbound {offering.display_name}"[:500],
        task_class=offering.capability[:100],
        reward=quoted_price,
        currency=currency,
        deadline=order.deadline,
        normalized_payload={
            "source_type": "INBOUND_SERVICE_ORDER",
            "revenue_channel": "PAY_PER_CALL_API" if offering.pricing_model == ServiceOffering.PricingModel.PER_CALL else "SERVICE_LISTING",
            "inbound_order_id": str(order.id),
            "operation": offering.operation,
            "requirements": requirements,
            "input_assets": assets,
        },
    )
    order.job = job
    order.save(update_fields=["job", "updated_at"])
    economics = EconomicsInput(
        gross_reward=quoted_price,
        marketplace_fee=platform_fee,
        p_acquire=Decimal("1"),
        p_accept=min(Decimal("1"), max(ZERO, _decimal(payload.get("acceptance_probability"), "0.95"))),
        p_payment=_inbound_payment_probability(order.funding_state),
        expected_genx_cost=offering.expected_genx_cost,
        expected_external_cost=offering.expected_external_cost,
        expected_compute_cost=offering.expected_operational_cost,
        estimated_worker_minutes=Decimal(max(1, offering.expected_minutes)),
    )
    score_and_persist(job, economics, recommended_offer=quoted_price, max_genx_credits=offering.max_genx_credits)
    run_inbound_economic_preflight(order)
    order.refresh_from_db()
    AuditEvent.objects.create(
        event_type="inbound.order_received",
        actor=f"market:{marketplace.slug}",
        metadata={"order_id": str(order.id), "job_id": str(job.id), "remote_order_id": remote_order_id, "idempotency_key": idempotency_key},
    )
    return order, True


@transaction.atomic
def run_inbound_economic_preflight(order: InboundOrder) -> AcquisitionPreflight:
    order = InboundOrder.objects.select_for_update().select_related("job", "listing__offering", "marketplace").get(pk=order.pk)
    job = order.job
    offering = order.listing.offering
    reasons = service_capability_blockers(offering)
    if order.listing.status != MarketServiceListing.Status.PUBLISHED:
        reasons.append("SERVICE_LISTING_NOT_PUBLISHED")
    market = order.marketplace
    if not market.enabled:
        reasons.append("MARKET_DISABLED")
    if market.status != Marketplace.Status.LIVE:
        reasons.append("MARKET_NOT_LIVE")
    if not market.payout_ready:
        reasons.append("PAYOUT_NOT_READY")
    if not market.south_africa_verified:
        reasons.append("SOUTH_AFRICA_PAYOUT_NOT_VERIFIED")
    try:
        profile = market.integration_profile
    except Exception:
        profile = None
    if profile is None or not profile.seller_capabilities.get("receive_orders"):
        reasons.append("RECEIVE_ORDERS_CAPABILITY_NOT_VERIFIED")
    elif profile.hosting_policy != "WEBDOCK_SAFE":
        reasons.append("MARKET_NOT_WEBDOCK_SAFE")
    policy = market.policy_versions.order_by("-checked_at", "-created_at").first()
    max_age = max(1, int(os.getenv("MARKET_POLICY_MAX_AGE_DAYS", "30")))
    if not policy or not policy.automation_allowed:
        reasons.append("MARKET_ORDER_POLICY_NOT_VERIFIED")
    elif policy.checked_at < timezone.now() - timedelta(days=max_age):
        reasons.append("MARKET_POLICY_STALE")
    if offering.max_genx_credits > 0:
        snapshot = GenXAccountSnapshot.objects.order_by("-created_at").first()
        if not snapshot or snapshot.available_credits is None:
            reasons.append("GENX_BALANCE_UNVERIFIED")
        elif snapshot.available_credits < offering.max_genx_credits:
            reasons.append("GENX_BUDGET_INSUFFICIENT")
    capacity = capture_capacity(persist=True)
    economic = evaluate_opportunity(job, capacity=capacity, capability=offering.capability)
    reasons.extend(economic.reason_codes)
    envelope = paid_cost_envelope(
        expected_gross=order.quoted_price,
        marketplace_fee=order.platform_fee,
        expected_genx_cost=offering.expected_genx_cost,
        expected_external_cost=offering.expected_external_cost,
        expected_operational_cost=offering.expected_operational_cost,
        risk_adjusted_profit=economic.risk_adjusted_profit,
        minimum_expected_profit=_decimal(os.getenv("MIN_EXPECTED_PROFIT_USD", "0")),
        absolute_max_paid_cost=_decimal(os.getenv("ABSOLUTE_MAX_PAID_COST_PER_JOB_USD", "250")),
        contingency_fraction=_decimal(os.getenv("PAID_COST_CONTINGENCY_FRACTION", "0.10")),
    )
    reasons.extend(envelope.reason_codes)
    reasons = list(dict.fromkeys(reasons))
    eligible = not reasons
    action_reasons = list(reasons)
    if os.getenv("INBOUND_SERVICE_AUTO_ACCEPT_ENABLED", "0") != "1":
        action_reasons.append("INBOUND_SERVICE_AUTO_ACCEPT_DISABLED")
    allowed = eligible and not action_reasons
    preflight = AcquisitionPreflight.objects.create(
        job=job,
        autonomy_mode=os.getenv("AUTONOMOUS_MODE", "OFF").upper(),
        operation=offering.operation,
        worker_class=offering.worker_class,
        eligible=eligible,
        allowed=allowed,
        reason_codes=list(dict.fromkeys(action_reasons)),
        expected_gross=order.quoted_price,
        marketplace_fee=order.platform_fee,
        genx_cost=offering.expected_genx_cost,
        operational_cost=offering.expected_operational_cost,
        expected_net=_money(envelope.expected_net_profit),
        confidence=job.jobscore.p_accept * job.jobscore.p_payment,
        details={
            "source": "INBOUND_SERVICE_ORDER",
            "fee_semantics": "PLATFORM_FEE_COUNTED_ONCE",
            "paid_cost_envelope": {
                "expected_paid_cost": str(envelope.expected_paid_cost),
                "expected_net_profit": str(envelope.expected_net_profit),
                "risk_adjusted_profit": str(envelope.risk_adjusted_profit),
            },
            "capacity_snapshot_id": str(capacity.id),
            "opportunity_cost": str(economic.opportunity_cost),
            "targets_are_caps": False,
        },
    )
    persist_opportunity_decision(job, economic, capacity=capacity, preflight=preflight, allowed=eligible, reason_codes=reasons)
    order.economic_preflight = {
        "preflight_id": str(preflight.id),
        "eligible": eligible,
        "action_allowed": allowed,
        "reason_codes": preflight.reason_codes,
        "expected_net_profit": str(envelope.expected_net_profit),
        "risk_adjusted_profit": str(economic.risk_adjusted_profit),
    }
    order.status = InboundOrder.Status.READY if eligible else InboundOrder.Status.PREFLIGHT_BLOCKED
    order.save(update_fields=["economic_preflight", "status", "updated_at"])
    AuditEvent.objects.create(
        severity="INFO" if eligible else "WARN",
        event_type="inbound.preflight_eligible" if eligible else "inbound.preflight_blocked",
        actor="profit-brain",
        metadata={"order_id": str(order.id), "job_id": str(job.id), "reason_codes": preflight.reason_codes},
    )
    return preflight


@transaction.atomic
def reconcile_inbound_settlement(
    order: InboundOrder,
    *,
    remote_event_id: str,
    state: str,
    gross: Decimal,
    fee: Decimal,
    currency: str,
    authoritative: bool,
    evidence_source: str,
    evidence: dict,
) -> tuple[InboundSettlementEvent, bool]:
    order = InboundOrder.objects.select_for_update().select_related("job", "marketplace").get(pk=order.pk)
    state = state.upper()
    if state not in InboundSettlementEvent.State.values:
        raise ValueError("INBOUND_SETTLEMENT_STATE_INVALID")
    if not remote_event_id or not evidence_source:
        raise ValueError("INBOUND_SETTLEMENT_EVIDENCE_REQUIRED")
    gross = _money(_decimal(gross)); fee = _money(_decimal(fee)); net = _money(gross - fee)
    if gross < 0 or fee < 0 or net < 0 or currency.upper() != order.currency:
        raise ValueError("INBOUND_SETTLEMENT_AMOUNT_INVALID")
    if authoritative and state in {InboundSettlementEvent.State.PAYOUT_PENDING, InboundSettlementEvent.State.SETTLED} and order.job.state not in {Job.State.ACCEPTED, Job.State.PAYOUT_PENDING}:
        raise ValueError("INBOUND_SETTLEMENT_JOB_NOT_ACCEPTED")
    if authoritative and state == InboundSettlementEvent.State.SETTLED:
        if not isinstance(evidence, dict) or evidence.get("irreversible") is not True:
            raise ValueError("AUTHORITATIVE_SETTLEMENT_PROOF_REQUIRED")
        if not order.marketplace.payout_ready or not order.marketplace.south_africa_verified:
            raise ValueError("AUTHORITATIVE_PAYOUT_ROUTE_NOT_READY")
    event, created = InboundSettlementEvent.objects.get_or_create(
        order=order,
        remote_event_id=remote_event_id,
        defaults={
            "state": state, "gross": gross, "fee": fee, "net": net, "currency": currency.upper(),
            "authoritative": authoritative, "evidence_source": evidence_source, "evidence": evidence,
        },
    )
    if not created:
        return event, False
    if authoritative and state in {InboundSettlementEvent.State.PAYOUT_PENDING, InboundSettlementEvent.State.SETTLED}:
        payout_state = Payout.State.PAYOUT_PENDING if state == InboundSettlementEvent.State.PAYOUT_PENDING else Payout.State.SETTLED
        now = timezone.now()
        payout, _ = Payout.objects.update_or_create(
            job=order.job,
            currency=currency.upper(),
            defaults={
                "gross": gross, "fee": fee, "net": net,
                "external_reference": remote_event_id,
                "state": payout_state,
                "earned_at": order.created_at,
                "pending_at": now,
                "settled_at": now if payout_state == Payout.State.SETTLED else None,
            },
        )
        if order.job.state == Job.State.ACCEPTED:
            transition_job(order.job_id, Job.State.PAYOUT_PENDING, actor="seller-settlement", metadata={"event_id": event.id})
            order.job.refresh_from_db()
        if payout_state == Payout.State.SETTLED and order.job.state == Job.State.PAYOUT_PENDING:
            transition_job(order.job_id, Job.State.SETTLED, actor="seller-settlement", metadata={"event_id": event.id})
            order.status = InboundOrder.Status.SETTLED
        else:
            order.status = InboundOrder.Status.PAYOUT_PENDING
        order.settlement_reference = remote_event_id
        order.save(update_fields=["status", "settlement_reference", "updated_at"])
    AuditEvent.objects.create(
        event_type="inbound.settlement_evidence_recorded",
        actor=evidence_source,
        metadata={
            "order_id": str(order.id), "event_id": event.id, "state": state,
            "authoritative": authoritative, "counts_as_cash": authoritative and state == "SETTLED",
        },
    )
    return event, True
