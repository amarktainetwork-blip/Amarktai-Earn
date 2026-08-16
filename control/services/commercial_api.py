from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_UP, Decimal, DecimalException
from pathlib import Path
from typing import Any

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from control.economics import EconomicsInput
from control.models import (
    ApifyChargedEvent,
    ApifyEventDefinition,
    AuditEvent,
    BuyerProfile,
    ChannelEconomicsVersion,
    CommercialAPIKey,
    CommercialAPIPlan,
    CommercialAPIProduct,
    CommercialAPIRequest,
    CommercialAPIUsage,
    Job,
    Marketplace,
    ServiceOffering,
)
from control.services.jobs import score_and_persist
from planning.services import (
    execute_work_plan,
    plan_awarded_job,
    stage_local_job_asset,
)
from workers.registry import operation_spec

ZERO = Decimal(0)
MONEY = Decimal("0.0001")
RAPIDAPI_SOURCE = "https://docs.rapidapi.com/do/docs/payouts-and-finance"
APIFY_SOURCE = "https://docs.apify.com/actors/publishing/monetize/pay-per-event"


class CommercialAPIError(ValueError):
    def __init__(self, code: str, *, status: int = 400, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail or _ERROR_DETAILS.get(code, "The request could not be processed safely.")


_ERROR_DETAILS = {
    "AUTHENTICATION_REQUIRED": "Supply a valid AmarktAI API key or verified marketplace proxy identity.",
    "API_KEY_REVOKED": "This API key has been revoked.",
    "API_KEY_EXPIRED": "This API key has expired.",
    "UNKNOWN_API_PRODUCT": "The requested commercial API product does not exist.",
    "PRODUCT_NOT_AVAILABLE": "This product is not enabled for authenticated requests.",
    "PLAN_NOT_ENTITLED": "The API key is not entitled to this product.",
    "IDEMPOTENCY_KEY_REQUIRED": "A stable Idempotency-Key header is required.",
    "IDEMPOTENCY_CONFLICT": "The idempotency key was already used with a different payload.",
    "REQUEST_TOO_LARGE": "The request exceeds the product payload limit.",
    "INVALID_SCHEMA": "The request body does not match the product schema.",
    "RATE_LIMIT_EXCEEDED": "The plan request rate has been exceeded.",
    "QUOTA_EXHAUSTED": "The plan quota has been exhausted.",
    "FREE_PLAN_PAID_EXECUTION_BLOCKED": "Free plans cannot trigger paid external execution.",
    "ECONOMIC_ADMISSION_REJECTED": "The request does not preserve the configured profit floor.",
    "EXECUTION_FAILED": "The canonical worker did not complete successfully.",
    "QA_NOT_PASSED": "The result is not available because independent QA did not pass.",
}


PRODUCT_DEFINITIONS = (
    {
        "slug": "structured-extraction",
        "display_name": "Structured Extraction API",
        "target_customer": "Operations teams turning unstructured business text into validated records",
        "problem_statement": "Extract schema-bound facts from supplied text without returning unvalidated free-form output.",
        "operation": "extract_structured_facts",
        "execution_class": CommercialAPIProduct.ExecutionClass.ASYNCHRONOUS,
        "input_schema": {"type": "object", "required": ["content", "schema"], "properties": {"content": {"type": "string", "maxLength": 100000}, "schema": {"type": "object"}}},
        "output_schema": {"type": "object", "properties": {"data": {"type": "object"}, "qa": {"type": "object"}}},
        "example": {"content": "Invoice 104 totals USD 42.00", "schema": {"invoice": "string", "total": "number"}},
        "expected_cost": "0.060000", "gross_price": "0.3000", "target_margin": "0.40",
        "proof_state": "READY_FOR_PRODUCTION_PROOF", "external_actions": ["GENX_CREDENTIAL_AND_LIVE_CATALOG_REQUIRED"],
    },
    {
        "slug": "data-cleanup",
        "display_name": "Data Cleanup API",
        "target_customer": "Developers and analysts normalizing repeat CSV or JSON feeds",
        "problem_statement": "Normalize headers and values, deduplicate rows, convert formats, map columns, or validate a schema.",
        "operation": "tabular_normalize",
        "execution_class": CommercialAPIProduct.ExecutionClass.SYNCHRONOUS,
        "input_schema": {"type": "object", "required": ["rows"], "properties": {"rows": {"type": "array", "maxItems": 5000}, "action": {"enum": ["normalize", "deduplicate", "convert", "column_map", "validate"]}, "column_mapping": {"type": "object"}, "schema": {"type": "object"}}},
        "output_schema": {"type": "object", "properties": {"rows": {"type": "array"}, "evidence": {"type": "object"}, "qa": {"type": "object"}}},
        "example": {"action": "normalize", "rows": [{" Name ": " Ada ", "Team": "Ops"}]},
        "expected_cost": "0.000000", "gross_price": "0.0200", "target_margin": "0.55",
        "proof_state": "ENGINEERING_PROVEN", "external_actions": ["DIRECT_API_KEY_ISSUANCE_OWNER_GATED", "RAPIDAPI_ACCOUNT_CREDENTIAL_REQUIRED"],
    },
    {
        "slug": "document-text",
        "display_name": "Document Text API",
        "target_customer": "Teams extracting searchable text from bounded documents",
        "problem_statement": "Extract text locally from supported documents and route OCR only when the source requires it.",
        "operation": "document_extract_text",
        "execution_class": CommercialAPIProduct.ExecutionClass.ASYNCHRONOUS,
        "input_schema": {"type": "object", "required": ["content_base64", "filename"], "properties": {"content_base64": {"type": "string"}, "filename": {"type": "string"}, "ocr": {"type": "boolean"}}},
        "output_schema": {"type": "object", "properties": {"text": {"type": "string"}, "qa": {"type": "object"}}},
        "example": {"filename": "brief.pdf", "content_base64": "<base64>"},
        "expected_cost": "0.002000", "gross_price": "0.0500", "target_margin": "0.55",
        "proof_state": "ENGINEERING_PROVEN", "external_actions": ["DIRECT_API_KEY_ISSUANCE_OWNER_GATED", "RAPIDAPI_ACCOUNT_CREDENTIAL_REQUIRED"],
    },
    {
        "slug": "public-web-extraction",
        "display_name": "Public Web Extraction API",
        "target_customer": "Teams collecting bounded public web data with explicit authorization and purpose",
        "problem_statement": "Extract allowed public page data with policy proof; heavy interactive browsing stays off-host through Apify.",
        "operation": "public_web_extract",
        "execution_class": CommercialAPIProduct.ExecutionClass.ASYNCHRONOUS,
        "input_schema": {"type": "object", "required": ["url", "purpose", "authorization_confirmed", "terms_permit"], "properties": {"url": {"type": "string", "format": "uri"}, "purpose": {"type": "string"}, "authorization_confirmed": {"const": True}, "terms_permit": {"const": True}}},
        "output_schema": {"type": "object", "properties": {"records": {"type": "array"}, "sources": {"type": "array"}, "qa": {"type": "object"}}},
        "example": {"url": "https://example.com", "purpose": "authorized product catalogue monitoring", "authorization_confirmed": True, "terms_permit": True},
        "expected_cost": "0.040000", "gross_price": "0.2500", "target_margin": "0.42",
        "proof_state": "READY_FOR_OWNER_ACTION", "external_actions": ["PUBLIC_WEB_POLICY_PROOF_REQUIRED", "APIFY_ACCOUNT_AND_STORE_PUBLICATION_REQUIRED"],
    },
    {
        "slug": "media-utility",
        "display_name": "Media Utility API",
        "target_customer": "Product teams needing bounded repeat image resizing, conversion, or compression",
        "problem_statement": "Run deterministic media utilities with decoded-size checks and QA before a result is released.",
        "operation": "image_resize",
        "execution_class": CommercialAPIProduct.ExecutionClass.ASYNCHRONOUS,
        "input_schema": {"type": "object", "required": ["content_base64", "filename", "width", "height"], "properties": {"content_base64": {"type": "string"}, "filename": {"type": "string"}, "width": {"type": "integer", "minimum": 1, "maximum": 10000}, "height": {"type": "integer", "minimum": 1, "maximum": 10000}, "output_format": {"enum": ["PNG", "JPEG", "WEBP"]}}},
        "output_schema": {"type": "object", "properties": {"artifact_base64": {"type": "string"}, "mime_type": {"type": "string"}, "qa": {"type": "object"}}},
        "example": {"filename": "product.png", "content_base64": "<base64>", "width": 800, "height": 800, "output_format": "WEBP"},
        "expected_cost": "0.003000", "gross_price": "0.0800", "target_margin": "0.55",
        "proof_state": "ENGINEERING_PROVEN", "external_actions": ["DIRECT_API_KEY_ISSUANCE_OWNER_GATED", "RAPIDAPI_ACCOUNT_CREDENTIAL_REQUIRED"],
    },
)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (DecimalException, TypeError, ValueError):
        return ZERO


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_UP)


def _json_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _safe_filename(value: str, default: str) -> str:
    name = Path(str(value or default)).name
    safe = "".join(char for char in name if char.isalnum() or char in "._-")[:120]
    return safe or default


def _uses_paid_provider(spec, operation: str) -> bool:
    return bool(spec.requires_genx and operation not in spec.local_operations)


def _error_schema() -> dict:
    return {"type": "object", "required": ["error", "request_id"], "properties": {"error": {"type": "object", "required": ["code", "message"]}, "request_id": {"type": "string"}}}


@transaction.atomic
def bootstrap_commercial_catalog() -> dict[str, int]:
    created = updated = 0
    for definition in PRODUCT_DEFINITIONS:
        spec = operation_spec(definition["operation"])
        expected_cost = _decimal(definition["expected_cost"])
        gross_price = _decimal(definition["gross_price"])
        minimum = _money(expected_cost / (Decimal(1) - _decimal(definition["target_margin"]))) if expected_cost else MONEY
        offering, _ = ServiceOffering.objects.update_or_create(
            slug=f"api-{definition['slug']}",
            defaults={
                "display_name": definition["display_name"],
                "description": definition["problem_statement"],
                "capability": definition["slug"],
                "operation": definition["operation"],
                "worker_class": spec.worker_class,
                "pricing_model": ServiceOffering.PricingModel.PER_CALL,
                "currency": "USD",
                "advertised_price": gross_price,
                "minimum_profitable_price": minimum,
                "platform_fee_rate": Decimal("0.25"),
                "expected_genx_cost": expected_cost if _uses_paid_provider(spec, definition["operation"]) else ZERO,
                "max_genx_credits": Decimal(10) if _uses_paid_provider(spec, definition["operation"]) else Decimal(0),
                "expected_external_cost": ZERO,
                "expected_operational_cost": Decimal("0.002") if _uses_paid_provider(spec, definition["operation"]) else expected_cost,
                "expected_minutes": 1,
                "sla_minutes": 1 if definition["execution_class"] == CommercialAPIProduct.ExecutionClass.SYNCHRONOUS else 30,
                "input_schema": definition["input_schema"],
                "output_schema": definition["output_schema"],
                "terms_metadata": {"commercial_api": True, "publication_owner_gated": True},
                "enabled": True,
                "accepting_orders": True,
                "proof_state": ServiceOffering.ProofState.SOURCE_PROVEN,
            },
        )
        product, was_created = CommercialAPIProduct.objects.update_or_create(
            slug=definition["slug"],
            defaults={
                "offering": offering,
                "display_name": definition["display_name"],
                "target_customer": definition["target_customer"],
                "problem_statement": definition["problem_statement"],
                "operation": definition["operation"],
                "worker_class": spec.worker_class,
                "execution_class": definition["execution_class"],
                "input_schema": definition["input_schema"],
                "output_schema": definition["output_schema"],
                "error_schema": _error_schema(),
                "examples": {"request": definition["example"]},
                "request_limits": {"max_bytes": 262144, "timeout_seconds": 30 if definition["execution_class"] == "SYNCHRONOUS" else 5},
                "quota_unit": "request",
                "pricing_unit": "successful_qa_approved_request",
                "expected_execution_cost": expected_cost,
                "gross_price": gross_price,
                "marketplace_fee_rate": Decimal("0.25"),
                "expected_net": _money(gross_price * Decimal("0.75") - expected_cost),
                "target_margin": _decimal(definition["target_margin"]),
                "minimum_profitable_price": minimum,
                "proof_state": definition["proof_state"],
                "publication_state": "READY_FOR_CREDENTIAL",
                "required_external_actions": definition["external_actions"],
                "enabled": True,
            },
        )
        created += int(was_created)
        updated += int(not was_created)
        plan_defaults = (
            ("basic", "BASIC", 100, "0.00", True),
            ("pro", "PRO", 1000, "0.00", False),
            ("ultra", "ULTRA", 10000, "0.00", False),
            ("mega", "MEGA", 50000, "0.00", False),
        )
        for slug, name, quota, price, is_free in plan_defaults:
            calculated = rapidapi_plan_economics(
                expected_calls=quota,
                per_call_cost=expected_cost,
                desired_margin=definition["target_margin"],
                operational_reserve=Decimal("0.02") if quota else ZERO,
                risk_reserve_rate=Decimal("0.05"),
                fee_rate=Decimal("0.25"),
            )
            if is_free:
                calculated["gross_price"] = Decimal(0)
            CommercialAPIPlan.objects.update_or_create(
                product=product, slug=slug, version=1,
                defaults={
                    "display_name": name,
                    "currency": "USD",
                    "monthly_price": _decimal(price) if price != "0.00" else calculated["gross_price"],
                    "monthly_quota": min(quota, 25) if is_free else quota,
                    "overage_price": max(product.minimum_profitable_price, product.gross_price),
                    "hard_usage_limit": True,
                    "requests_per_minute": 10 if is_free else 60,
                    "is_free": is_free,
                    "paid_external_execution_allowed": not is_free,
                    "minimum_margin": product.target_margin,
                    "economics": {key: str(value) for key, value in calculated.items()},
                    "active": True,
                },
            )

    ChannelEconomicsVersion.objects.update_or_create(
        channel="rapidapi", version="verified-2026-08-16",
        defaults={
            "source_url": RAPIDAPI_SOURCE, "checked_at": timezone.now(), "stale_after_days": 30,
            "marketplace_fee_rate": Decimal("0.25"), "creator_share_rate": Decimal("0.75"),
            "payout_rail": "PAYPAL", "verified": True, "active": True,
            "metadata": {"plans": ["BASIC", "PRO", "ULTRA", "MEGA", "PRIVATE_CUSTOM"], "overage_supported": True},
        },
    )
    ChannelEconomicsVersion.objects.update_or_create(
        channel="apify", version="verified-2026-08-16",
        defaults={
            "source_url": APIFY_SOURCE, "checked_at": timezone.now(), "stale_after_days": 30,
            "marketplace_fee_rate": Decimal("0.20"), "creator_share_rate": Decimal("0.80"),
            "payout_rail": "APIFY_CREATOR_PAYOUT", "verified": True, "active": True,
            "metadata": {"model": "PAY_PER_EVENT", "profit_formula": "creator_share_x_event_revenue_minus_platform_usage_costs"},
        },
    )
    for slug, name, unit, platform_cost, external_cost, price in (
        ("result-generated", "Result generated", "accepted result", "0.002", "0", "0.05"),
        ("page-processed", "Page processed", "public page", "0.004", "0", "0.08"),
        ("browser-page-processed", "Browser page processed", "interactive page", "0.020", "0.010", "0.20"),
        ("analysis-performed", "API/AI analysis performed", "QA-approved analysis", "0.010", "0.050", "0.35"),
    ):
        minimum_price = apify_event_economics(
            event_revenue=_decimal(price), platform_usage_cost=_decimal(platform_cost),
            external_cost=_decimal(external_cost), target_margin=Decimal("0.40"),
        )["minimum_price"]
        ApifyEventDefinition.objects.update_or_create(
            slug=slug, version=1,
            defaults={
                "display_name": name, "unit": unit,
                "expected_platform_cost": _decimal(platform_cost), "expected_external_cost": _decimal(external_cost),
                "price_per_event": _decimal(price), "minimum_price": minimum_price,
                "target_margin": Decimal("0.40"), "volume_tiers": [],
                "publication_state": "READY_FOR_OWNER_ACTION",
            },
        )
    return {"created": created, "updated": updated, "total": len(PRODUCT_DEFINITIONS)}


def active_channel_economics(channel: str, *, now=None) -> ChannelEconomicsVersion | None:
    now = now or timezone.now()
    row = ChannelEconomicsVersion.objects.filter(channel=channel, active=True, verified=True).order_by("-checked_at").first()
    if row is None or row.checked_at < now - timedelta(days=row.stale_after_days):
        return None
    return row


def rapidapi_plan_economics(*, expected_calls: int, per_call_cost: Decimal, desired_margin: Decimal | str, operational_reserve: Decimal = ZERO, payout_fx_reserve: Decimal = ZERO, risk_reserve_rate: Decimal = ZERO, fee_rate: Decimal | None = None) -> dict[str, Decimal]:
    if fee_rate is None:
        policy = active_channel_economics("rapidapi")
        if policy is None:
            raise CommercialAPIError("ECONOMICS_CATALOG_STALE", status=503, detail="RapidAPI economics are stale or unverified.")
        fee_rate = policy.marketplace_fee_rate
    fee_rate, margin = _decimal(fee_rate), _decimal(desired_margin)
    variable = _decimal(per_call_cost) * max(0, int(expected_calls))
    reserves = _decimal(operational_reserve) + _decimal(payout_fx_reserve) + variable * _decimal(risk_reserve_rate)
    denominator = Decimal(1) - fee_rate - margin
    if denominator <= 0:
        raise CommercialAPIError("ECONOMIC_ADMISSION_REJECTED")
    gross = _money((variable + reserves) / denominator)
    fee = _money(gross * fee_rate)
    net_receipts = gross - fee
    profit = net_receipts - variable - reserves
    return {"gross_price": gross, "marketplace_fee": fee, "net_receipts": net_receipts, "execution_cost": variable, "reserves": reserves, "expected_profit": profit, "expected_margin": profit / gross if gross else ZERO}


def rapidapi_export() -> dict:
    policy = active_channel_economics("rapidapi")
    products = CommercialAPIProduct.objects.prefetch_related("plans").order_by("slug")
    return {
        "channel": "rapidapi", "connection_state": "READY_FOR_CREDENTIAL",
        "publication_state": "READY_FOR_OWNER_ACTION", "published": False,
        "economics": None if policy is None else {
            "marketplace_fee_rate": str(policy.marketplace_fee_rate), "source": policy.source_url,
            "checked_at": policy.checked_at.isoformat(), "payout_rail": policy.payout_rail,
        },
        "products": [
            {
                "slug": product.slug, "title": product.display_name,
                "short_description": product.problem_statement[:180], "long_description": product.problem_statement,
                "categories": ["Data", "Business", "Developer Tools"], "tags": [product.worker_class, product.operation, "profit-aware"],
                "endpoints": [f"/api/v1/products/{product.slug}/jobs", "/api/v1/requests/{request_id}", "/api/v1/requests/{request_id}/result"],
                "plans": [{"name": plan.display_name, "monthly_price": str(plan.monthly_price), "quota": plan.monthly_quota, "overage": str(plan.overage_price), "hard_limit": plan.hard_usage_limit, "version": plan.version} for plan in product.plans.filter(active=True).order_by("monthly_quota")],
                "support": "Owner-gated support through the AmarktAI commercial contact path.",
                "faq": ["Outputs are released only after canonical QA.", "Idempotency is required for every submitted job."],
                "limitations": product.required_external_actions,
                "asset_requirements": ["square logo", "API product cover", "sample response screenshot"],
                "activation_blockers": product.required_external_actions,
            } for product in products
        ],
    }


def apify_event_economics(*, event_revenue: Decimal, platform_usage_cost: Decimal, external_cost: Decimal = ZERO, target_margin: Decimal = Decimal("0.40"), creator_share: Decimal | None = None) -> dict[str, Decimal]:
    if creator_share is None:
        policy = active_channel_economics("apify")
        if policy is None:
            raise CommercialAPIError(
                "ECONOMICS_CATALOG_STALE",
                status=503,
                detail="Apify economics are stale or unverified.",
            )
        creator_share = policy.creator_share_rate
    creator_share = _decimal(creator_share)
    revenue, costs = _decimal(event_revenue), _decimal(platform_usage_cost) + _decimal(external_cost)
    profit = revenue * creator_share - costs
    denominator = creator_share - _decimal(target_margin)
    minimum = _money(costs / denominator) if denominator > 0 else Decimal(999999)
    return {"event_revenue": revenue, "creator_receipt": revenue * creator_share, "total_variable_cost": costs, "expected_profit": profit, "expected_margin": profit / revenue if revenue else ZERO, "minimum_price": minimum}


def apify_export() -> dict:
    policy = active_channel_economics("apify")
    return {
        "channel": "apify", "model": "PAY_PER_EVENT", "published": False,
        "publication_state": "READY_FOR_OWNER_ACTION", "connection_state": "READY_FOR_CREDENTIAL",
        "economics": None if policy is None else {"creator_share_rate": str(policy.creator_share_rate), "source": policy.source_url, "checked_at": policy.checked_at.isoformat()},
        "events": [{"slug": row.slug, "name": row.display_name, "unit": row.unit, "price": str(row.price_per_event), "minimum_price": str(row.minimum_price), "target_margin": str(row.target_margin), "publication_state": row.publication_state} for row in ApifyEventDefinition.objects.order_by("slug")],
        "activation_blockers": ["APIFY_ACCOUNT_CREDENTIAL_REQUIRED", "APIFY_STORE_OWNER_PUBLICATION_REQUIRED", "APIFY_PAYOUT_PROOF_REQUIRED"],
    }


@transaction.atomic
def record_apify_charge(*, definition: ApifyEventDefinition, run_reference: str, charge_identity: str, units: int, platform_usage_cost: Decimal, external_cost: Decimal = ZERO) -> tuple[ApifyChargedEvent, bool]:
    if not run_reference or not charge_identity or units < 1:
        raise CommercialAPIError("INVALID_SCHEMA")
    revenue = definition.price_per_event * units
    row, created = ApifyChargedEvent.objects.get_or_create(
        definition=definition, charge_identity=charge_identity,
        defaults={"run_reference": run_reference[:160], "units": units, "event_revenue": revenue, "platform_usage_cost": platform_usage_cost, "external_cost": external_cost},
    )
    if not created and (row.run_reference != run_reference[:160] or row.units != units):
        raise CommercialAPIError("IDEMPOTENCY_CONFLICT")
    return row, created


def create_api_key(*, buyer: BuyerProfile, plan: CommercialAPIPlan, label: str = "") -> tuple[CommercialAPIKey, str]:
    prefix = secrets.token_hex(5)
    secret = secrets.token_urlsafe(32)
    row = CommercialAPIKey.objects.create(prefix=prefix, secret_hash=make_password(secret), buyer=buyer, plan=plan, label=label[:120])
    return row, f"ak_{prefix}.{secret}"


@dataclass(frozen=True)
class AuthenticatedAPIIdentity:
    key: CommercialAPIKey
    source: str


def authenticate_api_key(raw: str) -> AuthenticatedAPIIdentity:
    if not raw.startswith("ak_") or "." not in raw:
        raise CommercialAPIError("AUTHENTICATION_REQUIRED", status=401)
    prefix, secret = raw[3:].split(".", 1)
    row = CommercialAPIKey.objects.select_related("plan__product", "buyer").filter(prefix=prefix).first()
    if row is None or not check_password(secret, row.secret_hash):
        raise CommercialAPIError("AUTHENTICATION_REQUIRED", status=401)
    if row.revoked_at:
        raise CommercialAPIError("API_KEY_REVOKED", status=401)
    if row.expires_at and row.expires_at <= timezone.now():
        raise CommercialAPIError("API_KEY_EXPIRED", status=401)
    if not row.plan.active or not row.plan.product.enabled:
        raise CommercialAPIError("PLAN_NOT_ENTITLED", status=403)
    return AuthenticatedAPIIdentity(row, "DIRECT_API_KEY")


def authenticate_request(request, product: CommercialAPIProduct) -> AuthenticatedAPIIdentity:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return authenticate_api_key(auth[7:].strip())
    proxy_secret = request.headers.get("X-RapidAPI-Proxy-Secret", "")
    expected = os.getenv("RAPIDAPI_PROXY_SECRET", "")
    if proxy_secret and expected and hmac.compare_digest(proxy_secret, expected):
        buyer_ref = request.headers.get("X-RapidAPI-User", "")
        if not buyer_ref:
            raise CommercialAPIError("AUTHENTICATION_REQUIRED", status=401)
        buyer = buyer_for_external_reference(channel="rapidapi", external_reference=buyer_ref)
        plan = CommercialAPIPlan.objects.filter(product=product, slug=(request.headers.get("X-RapidAPI-Subscription", "basic").lower()), active=True).order_by("-version").first()
        if plan is None:
            raise CommercialAPIError("PLAN_NOT_ENTITLED", status=403)
        identity_digest = hashlib.sha256(f"{buyer.id}:{product.id}:{plan.id}".encode()).hexdigest()[:14]
        key, _ = CommercialAPIKey.objects.get_or_create(
            prefix=f"rapid-{identity_digest}",
            defaults={
                "secret_hash": make_password(secrets.token_urlsafe(32)),
                "buyer": buyer,
                "plan": plan,
                "label": "RapidAPI proxy identity",
            },
        )
        return AuthenticatedAPIIdentity(key, "RAPIDAPI_PROXY")
    raise CommercialAPIError("AUTHENTICATION_REQUIRED", status=401)


def buyer_for_external_reference(*, channel: str, external_reference: str) -> BuyerProfile:
    pepper = os.getenv("CUSTOMER_REFERENCE_PEPPER", os.getenv("AUTH_THROTTLE_PEPPER", "development-customer-reference-pepper"))
    digest = hmac.new(pepper.encode(), f"{channel}:{external_reference}".encode(), hashlib.sha256).hexdigest()
    row, _ = BuyerProfile.objects.get_or_create(channel=channel[:80], external_reference_hash=digest)
    return row


def revoke_api_key(row: CommercialAPIKey) -> CommercialAPIKey:
    row.revoked_at = timezone.now()
    row.save(update_fields=["revoked_at", "updated_at"])
    AuditEvent.objects.create(event_type="commercial.api_key_revoked", actor="owner", metadata={"api_key_id": str(row.id), "prefix": row.prefix})
    return row


def _validate_payload(product: CommercialAPIProduct, payload: dict) -> None:
    if not isinstance(payload, dict):
        raise CommercialAPIError("INVALID_SCHEMA")
    encoded = json.dumps(payload, separators=(",", ":"), default=str).encode()
    max_bytes = int((product.request_limits or {}).get("max_bytes") or 262144)
    if len(encoded) > max_bytes:
        raise CommercialAPIError("REQUEST_TOO_LARGE", status=413)
    required = (product.input_schema or {}).get("required") or []
    if any(field not in payload for field in required):
        raise CommercialAPIError("INVALID_SCHEMA")
    if product.slug == "data-cleanup":
        rows = payload.get("rows")
        if not isinstance(rows, list) or len(rows) > 5000 or not all(isinstance(row, dict) for row in rows):
            raise CommercialAPIError("INVALID_SCHEMA")
        if payload.get("action", "normalize") not in {"normalize", "deduplicate", "convert", "column_map", "validate"}:
            raise CommercialAPIError("INVALID_SCHEMA")
    if product.slug in {"document-text", "media-utility"}:
        try:
            content = base64.b64decode(str(payload.get("content_base64") or ""), validate=True)
        except Exception as exc:
            raise CommercialAPIError("INVALID_SCHEMA") from exc
        if not content or len(content) > max_bytes:
            raise CommercialAPIError("REQUEST_TOO_LARGE", status=413)
    if product.slug == "public-web-extraction" and (payload.get("authorization_confirmed") is not True or payload.get("terms_permit") is not True):
        raise CommercialAPIError("INVALID_SCHEMA")


def _quota_and_rate(identity: AuthenticatedAPIIdentity, product: CommercialAPIProduct) -> None:
    plan, now = identity.key.plan, timezone.now()
    if plan.product_id != product.id:
        raise CommercialAPIError("PLAN_NOT_ENTITLED", status=403)
    recent = CommercialAPIRequest.objects.filter(api_key=identity.key, created_at__gte=now - timedelta(minutes=1)).count()
    if recent >= plan.requests_per_minute:
        raise CommercialAPIError("RATE_LIMIT_EXCEEDED", status=429)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    admitted = CommercialAPIRequest.objects.filter(
        api_key__buyer=identity.key.buyer,
        api_key__plan=plan,
        created_at__gte=period_start,
    ).count()
    if plan.hard_usage_limit and admitted >= plan.monthly_quota:
        raise CommercialAPIError("QUOTA_EXHAUSTED", status=429)
    spec = operation_spec(product.operation)
    if plan.is_free and _uses_paid_provider(spec, product.operation) and not plan.paid_external_execution_allowed:
        raise CommercialAPIError("FREE_PLAN_PAID_EXECUTION_BLOCKED", status=403)
    fee_rate = _fee_rate(identity, product)
    unit_receipt = plan.overage_price * (Decimal(1) - fee_rate)
    required_profit = plan.overage_price * plan.minimum_margin
    if plan.overage_price and unit_receipt - product.expected_execution_cost < required_profit:
        raise CommercialAPIError("ECONOMIC_ADMISSION_REJECTED", status=403)


def _fee_rate(identity: AuthenticatedAPIIdentity, product: CommercialAPIProduct) -> Decimal:
    if identity.source != "RAPIDAPI_PROXY":
        return product.marketplace_fee_rate
    policy = active_channel_economics("rapidapi")
    if policy is None:
        raise CommercialAPIError(
            "ECONOMICS_CATALOG_STALE",
            status=503,
            detail="RapidAPI economics are stale or unverified.",
        )
    return policy.marketplace_fee_rate


def _operation_inputs(product: CommercialAPIProduct, payload: dict) -> tuple[str, dict, bytes | None, str]:
    operation, inputs, content, filename = product.operation, {}, None, "input.json"
    if product.slug == "data-cleanup":
        action = payload.get("action", "normalize")
        operation = {"normalize": "tabular_normalize", "deduplicate": "tabular_deduplicate", "convert": "tabular_convert", "column_map": "tabular_column_map", "validate": "tabular_schema_validate"}[action]
        inputs = {"output_format": "json", "column_mapping": payload.get("column_mapping") or {}, "schema": payload.get("schema") or {}}
        content = json.dumps(payload["rows"], ensure_ascii=False).encode()
    elif product.slug in {"document-text", "media-utility"}:
        content = base64.b64decode(payload["content_base64"], validate=True)
        filename = _safe_filename(payload.get("filename"), "input.bin")
        inputs = {key: value for key, value in payload.items() if key not in {"content_base64", "filename"}}
        if product.slug == "document-text" and payload.get("ocr"):
            operation = "ocr_document"
    else:
        inputs = dict(payload)
    inputs["operation"] = operation
    return operation, inputs, content, filename


def _stage_job_input(request_row: CommercialAPIRequest, content: bytes, filename: str) -> None:
    upload_root = Path(os.getenv("AMARKTAI_UPLOAD_ROOT", "/var/lib/amarktai-earn/uploads"))
    target_dir = upload_root / "commercial-api" / str(request_row.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    target.write_bytes(content)
    stage_local_job_asset(job_id=request_row.job_id, path=str(target), source="commercial_api", external_id=str(request_row.id), semantic_role="source")


@transaction.atomic
def admit_request(*, identity: AuthenticatedAPIIdentity, product: CommercialAPIProduct, idempotency_key: str, payload: dict, correlation_id: str) -> tuple[CommercialAPIRequest, bool]:
    if not idempotency_key.strip():
        raise CommercialAPIError("IDEMPOTENCY_KEY_REQUIRED")
    if len(idempotency_key) > 180:
        raise CommercialAPIError("INVALID_SCHEMA", detail="Idempotency-Key must not exceed 180 characters.")
    _validate_payload(product, payload)
    digest = _json_digest(payload)
    existing = CommercialAPIRequest.objects.select_for_update().filter(api_key=identity.key, idempotency_key=idempotency_key).first()
    if existing:
        if existing.request_digest != digest or existing.product_id != product.id:
            raise CommercialAPIError("IDEMPOTENCY_CONFLICT", status=409)
        return existing, False
    _quota_and_rate(identity, product)
    marketplace, _ = Marketplace.objects.get_or_create(
        slug="amarktai-direct-api",
        defaults={"display_name": "AmarktAI Direct API", "status": Marketplace.Status.WATCH_ONLY, "enabled": False, "payout_ready": False, "south_africa_verified": False, "fee_rate": ZERO, "payment_model": "OWNER_GATED_API_ENTITLEMENT"},
    )
    row = CommercialAPIRequest.objects.create(
        api_key=identity.key, product=product, idempotency_key=idempotency_key[:180], request_digest=digest,
        correlation_id=correlation_id[:64], input_payload=payload, estimated_cost=product.expected_execution_cost,
        status=CommercialAPIRequest.Status.ADMITTED,
    )
    job = Job.objects.create(
        marketplace=marketplace, external_id=f"commercial-api:{row.id}", title=f"API: {product.display_name}",
        task_class=product.slug, reward=product.gross_price, currency="USD", state=Job.State.AWARDED,
        normalized_payload={"source_type": "COMMERCIAL_API", "commercial_api_request_id": str(row.id), "operation": product.operation, "inputs": payload},
    )
    row.job = job
    row.save(update_fields=["job", "updated_at"])
    fee_rate = _fee_rate(identity, product)
    score_and_persist(job, EconomicsInput(
        gross_reward=product.gross_price, marketplace_fee=product.gross_price * fee_rate,
        p_acquire=Decimal(1), p_accept=Decimal("0.99"), p_payment=Decimal("0.50"),
        expected_genx_cost=product.expected_execution_cost if _uses_paid_provider(operation_spec(product.operation), product.operation) else ZERO,
        expected_external_cost=ZERO, expected_compute_cost=ZERO if _uses_paid_provider(operation_spec(product.operation), product.operation) else product.expected_execution_cost,
        estimated_worker_minutes=Decimal(1),
    ), decision="API_ADMITTED", reason_codes=[], max_genx_credits=product.offering.max_genx_credits, recommended_offer=product.gross_price)
    identity.key.last_used_at = timezone.now()
    identity.key.save(update_fields=["last_used_at", "updated_at"])
    AuditEvent.objects.create(event_type="commercial.api_request_admitted", actor=f"api:{identity.source.lower()}", metadata={"request_id": str(row.id), "product": product.slug, "job_id": str(job.id), "correlation_id": row.correlation_id})
    return row, True


def execute_request(row: CommercialAPIRequest) -> CommercialAPIRequest:
    if row.status in {CommercialAPIRequest.Status.COMPLETED, CommercialAPIRequest.Status.FAILED}:
        return row
    operation, inputs, content, filename = _operation_inputs(row.product, row.input_payload)
    if content is not None and not row.job.assets.exists():
        _stage_job_input(row, content, filename)
    payload = dict(row.job.normalized_payload or {})
    payload["operation"] = operation
    payload["inputs"] = inputs
    row.job.normalized_payload = payload
    row.job.save(update_fields=["normalized_payload", "updated_at"])
    try:
        plan = plan_awarded_job(row.job_id)
        row.work_plan_id = plan.id
        if plan.status != "READY":
            raise CommercialAPIError("EXECUTION_FAILED", detail=",".join(plan.reason_codes) or "The canonical plan was blocked.")
        row.status = CommercialAPIRequest.Status.EXECUTING
        row.save(update_fields=["work_plan_id", "status", "updated_at"])
        plan = execute_work_plan(plan.id)
        execution = row.job.executions.order_by("-attempt").first()
        if plan.status != "QA_PASSED" or execution is None or execution.status != "QA_PASSED":
            raise CommercialAPIError("QA_NOT_PASSED")
        artifact = row.job.artifacts.filter(accepted=True).order_by("id").first()
        if artifact is None:
            raise CommercialAPIError("QA_NOT_PASSED")
        path = Path(artifact.path)
        result_payload: dict[str, Any] = {"artifact": {"name": path.name, "mime_type": artifact.mime_type, "sha256": artifact.sha256, "size_bytes": artifact.size_bytes}, "evidence": execution.result.get("worker_evidence", {}), "qa": execution.result.get("qa", {})}
        if artifact.mime_type == "application/json" or path.suffix == ".json":
            result_payload["data"] = json.loads(path.read_text(encoding="utf-8"))
        elif artifact.mime_type.startswith("text/") or path.suffix in {".csv", ".txt", ".md"}:
            result_payload["text"] = path.read_text(encoding="utf-8")
        else:
            result_payload["artifact_base64"] = base64.b64encode(path.read_bytes()).decode()
        actual_cost = sum((_decimal(call.cost_equivalent) for call in row.job.genxcall_set.all() if call.cost_equivalent is not None), ZERO)
        row.status = CommercialAPIRequest.Status.COMPLETED
        row.qa_passed = True
        row.result_payload = result_payload
        row.actual_cost = actual_cost
        row.completed_at = timezone.now()
        row.error_code = ""
        row.error_detail = ""
        row.save()
        _record_usage_once(row)
    except Exception as exc:  # noqa: BLE001 - persist a safe terminal state for every worker failure
        row.status = CommercialAPIRequest.Status.FAILED
        row.qa_passed = False
        row.error_code = exc.code if isinstance(exc, CommercialAPIError) else "EXECUTION_FAILED"
        row.error_detail = exc.detail if isinstance(exc, CommercialAPIError) else "The canonical execution failed safely."
        row.completed_at = timezone.now()
        row.save(update_fields=["status", "qa_passed", "error_code", "error_detail", "completed_at", "updated_at"])
        AuditEvent.objects.create(
            event_type="commercial.api_request_failed",
            actor="commercial-api",
            metadata={"request_id": str(row.id), "error_code": row.error_code, "exception_class": exc.__class__.__name__},
        )
    return CommercialAPIRequest.objects.select_related("product", "api_key__plan").get(pk=row.pk)


@transaction.atomic
def _record_usage_once(row: CommercialAPIRequest) -> CommercialAPIUsage:
    existing = CommercialAPIUsage.objects.filter(request=row).first()
    if existing:
        return existing
    plan = row.api_key.plan
    gross = plan.overage_price * row.quota_units
    fee = gross * _fee_rate(AuthenticatedAPIIdentity(row.api_key, "RAPIDAPI_PROXY" if row.api_key.prefix.startswith("rapid-") else "DIRECT_API_KEY"), row.product)
    cost = row.actual_cost if row.actual_cost is not None else row.estimated_cost
    usage = CommercialAPIUsage.objects.create(
        request=row, buyer=row.api_key.buyer, product=row.product, plan=plan, units=row.quota_units,
        gross_billed=gross, marketplace_fee=fee, execution_cost=cost,
        settled_revenue=ZERO, settled_net_profit=ZERO, authoritative_settlement=False,
    )
    AuditEvent.objects.create(event_type="commercial.api_usage_metered", actor="commercial-api", metadata={"request_id": str(row.id), "usage_id": usage.id, "units": usage.units, "settlement_state": "NOT_SETTLED"})
    return usage


def request_payload(row: CommercialAPIRequest, *, include_result: bool = False) -> dict:
    payload = {
        "request_id": str(row.id), "correlation_id": row.correlation_id, "product": row.product.slug,
        "status": row.status, "job_id": str(row.job_id) if row.job_id else None,
        "work_plan_id": row.work_plan_id, "qa_passed": row.qa_passed,
        "created_at": row.created_at.isoformat(), "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "error": None if not row.error_code else {"code": row.error_code, "message": row.error_detail or _ERROR_DETAILS.get(row.error_code, "Request failed safely.")},
    }
    if include_result and row.status == CommercialAPIRequest.Status.COMPLETED and row.qa_passed:
        payload["result"] = row.result_payload
    return payload


def openapi_spec(request=None) -> dict:
    products = CommercialAPIProduct.objects.filter(enabled=True).order_by("slug")
    base = request.build_absolute_uri("/").rstrip("/") if request else "https://earn.amarktai.co.za"
    paths: dict[str, Any] = {
        "/api/v1/requests/{request_id}": {"get": {"summary": "Get request status", "security": [{"BearerAPIKey": []}], "parameters": [{"name": "request_id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}], "responses": {"200": {"description": "Canonical request state"}, "401": {"$ref": "#/components/responses/AuthenticationError"}}}},
        "/api/v1/requests/{request_id}/result": {"get": {"summary": "Get a QA-approved result", "security": [{"BearerAPIKey": []}], "parameters": [{"name": "request_id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}], "responses": {"200": {"description": "QA-approved result"}, "409": {"description": "Result is not complete"}}}},
    }
    for product in products:
        paths[f"/api/v1/products/{product.slug}/jobs"] = {"post": {
            "summary": product.display_name, "description": product.problem_statement,
            "security": [{"BearerAPIKey": []}, {"RapidAPIProxy": []}],
            "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string", "maxLength": 180}}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": product.input_schema, "example": (product.examples or {}).get("request")}}},
            "responses": {"200": {"description": "Synchronous QA-approved result"}, "202": {"description": "Accepted asynchronous job"}, "400": {"description": "Structured request error"}, "401": {"$ref": "#/components/responses/AuthenticationError"}, "409": {"description": "Idempotency conflict"}, "429": {"description": "Rate or quota limit"}},
        }}
    return {
        "openapi": "3.1.0",
        "info": {"title": "AmarktAI Earn Commercial API", "version": "1.0.0", "description": "Profit-aware commercial API products. Every output follows canonical execution and independent QA; settlement remains separate."},
        "servers": [{"url": base}], "paths": paths,
        "components": {"securitySchemes": {"BearerAPIKey": {"type": "http", "scheme": "bearer", "bearerFormat": "ak_prefix.secret"}, "RapidAPIProxy": {"type": "apiKey", "in": "header", "name": "X-RapidAPI-Proxy-Secret"}}, "responses": {"AuthenticationError": {"description": "Authentication failed", "content": {"application/json": {"schema": _error_schema()}}}}},
        "x-amarktai": {"idempotency_required": True, "quota_metering": "successful request exactly once", "async_lifecycle": ["ADMITTED", "QUEUED", "EXECUTING", "QA_PASSED", "COMPLETED"], "support": "/api/docs/#support", "settlement_truth": "API usage is not settled revenue"},
    }
