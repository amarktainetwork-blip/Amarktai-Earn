from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import (
    BuyerProfile,
    ChannelEconomicsVersion,
    CommercialAPIPlan,
    CommercialAPIProduct,
)
from control.services.commercial_api import (
    active_channel_economics,
    bootstrap_commercial_catalog,
    create_api_key,
    openapi_spec,
)

API_MARKET_SOURCE = "https://api.market/seller"
API_MARKET_PRODUCT_SOURCE = "https://docs.api.market/seller-docs/what-is-an-api-product"
API_MARKET_USAGE_SOURCE = "https://docs.api.market/seller-docs/custom-usage"
ZYLA_SOURCE = "https://zylalabs.com/monetize-your-api"
POSTMAN_SOURCE = "https://learning.postman.com/docs/postman-api-network/showcase/publish/public-apis"
POSTMAN_PREPARE_SOURCE = "https://learning.postman.com/docs/postman-api-network/showcase/prepare/public-collections/"

API_MARKET_CHANNEL = "api-market"
ZYLA_CHANNEL = "zyla-api-hub"
POSTMAN_CHANNEL = "postman-api-network"

API_MARKET_BACKEND_PLAN = "api-market-backend"
ZYLA_BACKEND_PLAN = "zyla-backend"

API_MARKET_KEY_LABEL_PREFIX = "API.market backend"
ZYLA_KEY_LABEL_PREFIX = "Zyla backend"

# Zyla's public provider surface is subscription/request oriented and its public
# consumer contract uses one Zyla bearer key. Until a stable provider-side caller
# identity/async-result billing contract is proven, expose only deterministic
# synchronous products through this channel. That keeps result ownership and
# usage accounting fail-closed instead of guessing at an undocumented contract.
ZYLA_LAUNCH_PRODUCT_SLUGS = frozenset({"data-cleanup"})


def marketplace_key_source(label: str) -> str:
    normalized = str(label or "").strip().casefold()
    if normalized.startswith(API_MARKET_KEY_LABEL_PREFIX.casefold()):
        return "API_MARKET_PROXY"
    if normalized.startswith(ZYLA_KEY_LABEL_PREFIX.casefold()):
        return "ZYLA_PROXY"
    return "DIRECT_API_KEY"


def _marketplace_backend_plan(*, product: CommercialAPIProduct, channel: str) -> CommercialAPIPlan:
    if channel == API_MARKET_CHANNEL:
        slug = API_MARKET_BACKEND_PLAN
        display_name = "API.market backend"
        paid_external_execution_allowed = True
    elif channel == ZYLA_CHANNEL:
        if product.slug not in ZYLA_LAUNCH_PRODUCT_SLUGS:
            raise ValueError("ZYLA_PRODUCT_NOT_LAUNCH_APPROVED")
        slug = ZYLA_BACKEND_PLAN
        display_name = "Zyla backend"
        paid_external_execution_allowed = False
    else:
        raise KeyError("UNKNOWN_API_DISTRIBUTION_CHANNEL")

    plan, _ = CommercialAPIPlan.objects.update_or_create(
        product=product,
        slug=slug,
        version=1,
        defaults={
            "display_name": display_name,
            "currency": "USD",
            "monthly_price": Decimal("0"),
            "monthly_quota": 1_000_000,
            "overage_price": max(product.minimum_profitable_price, product.gross_price),
            "hard_usage_limit": False,
            "requests_per_minute": 300,
            "is_free": False,
            "paid_external_execution_allowed": paid_external_execution_allowed,
            "minimum_margin": product.target_margin,
            "economics": {
                "purpose": "internal_marketplace_gateway_entitlement",
                "external_billing_authoritative": True,
                "channel": channel,
            },
            "active": True,
        },
    )
    return plan


@transaction.atomic
def bootstrap_api_distribution() -> dict[str, Any]:
    bootstrap_commercial_catalog()

    ChannelEconomicsVersion.objects.update_or_create(
        channel=API_MARKET_CHANNEL,
        version="verified-2026-08-18",
        defaults={
            "source_url": API_MARKET_SOURCE,
            "checked_at": timezone.now(),
            "stale_after_days": 30,
            "marketplace_fee_rate": Decimal("0.20"),
            "creator_share_rate": Decimal("0.80"),
            "payout_rail": "GLOBAL_PROVIDER_PAYOUT_OWNER_PROOF_REQUIRED",
            "verified": True,
            "active": True,
            "metadata": {
                "free_to_list": True,
                "managed_gateway": True,
                "managed_billing": True,
                "mcp_endpoint_generated_by_marketplace": True,
                "rate_limit_seller_enforcement_required": True,
                "seller_contract_source": API_MARKET_PRODUCT_SOURCE,
                "custom_usage_source": API_MARKET_USAGE_SOURCE,
                "south_africa_payout_account_proof_required": True,
            },
        },
    )
    ChannelEconomicsVersion.objects.update_or_create(
        channel=ZYLA_CHANNEL,
        version="verified-2026-08-18",
        defaults={
            "source_url": ZYLA_SOURCE,
            "checked_at": timezone.now(),
            "stale_after_days": 30,
            # Top-tier headline share is 80%, then payment-processing/refund/output
            # costs apply. Use a deliberately conservative 25% effective reserve
            # for admission while keeping the exact published formula in metadata.
            "marketplace_fee_rate": Decimal("0.25"),
            "creator_share_rate": Decimal("0.75"),
            "payout_rail": "PAYPAL",
            "verified": True,
            "active": True,
            "metadata": {
                "headline_provider_share": "0.80",
                "headline_platform_share": "0.20",
                "input_processing_fee_estimate": "0.03",
                "output_processing_fee": "0.02",
                "payout_delay_days": 60,
                "uptime_minimum": "0.998",
                "provider_share_varies_with_uptime": True,
                "payout_requires_owner_request": True,
                "south_africa_paypal_receipt_proof_required": True,
            },
        },
    )

    products = list(CommercialAPIProduct.objects.filter(enabled=True).order_by("slug"))
    api_market_plans = 0
    zyla_plans = 0
    for product in products:
        _marketplace_backend_plan(product=product, channel=API_MARKET_CHANNEL)
        api_market_plans += 1
        if product.slug in ZYLA_LAUNCH_PRODUCT_SLUGS:
            _marketplace_backend_plan(product=product, channel=ZYLA_CHANNEL)
            zyla_plans += 1

    return {
        "products": len(products),
        "api_market_backend_plans": api_market_plans,
        "zyla_backend_plans": zyla_plans,
        "postman_publication_mutation": False,
    }


def issue_marketplace_backend_key(*, channel: str, product_slug: str) -> tuple[Any, str]:
    if channel not in {API_MARKET_CHANNEL, ZYLA_CHANNEL}:
        raise KeyError("UNKNOWN_API_DISTRIBUTION_CHANNEL")
    product = CommercialAPIProduct.objects.get(slug=product_slug, enabled=True)
    if channel == ZYLA_CHANNEL and product.slug not in ZYLA_LAUNCH_PRODUCT_SLUGS:
        raise ValueError("ZYLA_PRODUCT_NOT_LAUNCH_APPROVED")
    plan = _marketplace_backend_plan(product=product, channel=channel)
    buyer, _ = BuyerProfile.objects.get_or_create(
        channel=channel,
        external_reference_hash=f"marketplace-aggregate:{channel}:{product.slug}"[:64],
    )
    label_prefix = API_MARKET_KEY_LABEL_PREFIX if channel == API_MARKET_CHANNEL else ZYLA_KEY_LABEL_PREFIX
    return create_api_key(buyer=buyer, plan=plan, label=f"{label_prefix}: {product.slug}")


def _plan_rows(product: CommercialAPIProduct) -> list[dict[str, Any]]:
    excluded = {API_MARKET_BACKEND_PLAN, ZYLA_BACKEND_PLAN}
    return [
        {
            "slug": plan.slug,
            "name": plan.display_name,
            "monthly_price": str(plan.monthly_price),
            "monthly_quota": plan.monthly_quota,
            "overage_price": str(plan.overage_price),
            "hard_limit": plan.hard_usage_limit,
            "requests_per_minute": plan.requests_per_minute,
            "free": plan.is_free,
            "version": plan.version,
        }
        for plan in product.plans.filter(active=True).exclude(slug__in=excluded).order_by("monthly_quota", "slug")
    ]


def _product_row(product: CommercialAPIProduct) -> dict[str, Any]:
    return {
        "slug": product.slug,
        "title": product.display_name,
        "description": product.problem_statement,
        "execution_class": product.execution_class,
        "operation": product.operation,
        "submit_path": f"/api/v1/products/{product.slug}/jobs",
        "status_path": "/api/v1/requests/{request_id}",
        "result_path": "/api/v1/requests/{request_id}/result",
        "input_schema": product.input_schema,
        "output_schema": product.output_schema,
        "example": (product.examples or {}).get("request", {}),
        "plans": _plan_rows(product),
        "proof_state": product.proof_state,
        "required_external_actions": list(product.required_external_actions or []),
    }


def api_market_export() -> dict[str, Any]:
    policy = active_channel_economics(API_MARKET_CHANNEL)
    products = list(CommercialAPIProduct.objects.filter(enabled=True).prefetch_related("plans").order_by("slug"))
    return {
        "channel": API_MARKET_CHANNEL,
        "role": "PAID_API_AND_MCP_STOREFRONT",
        "published": False,
        "external_mutation_allowed": False,
        "connection_state": "READY_FOR_OWNER_ACTION",
        "publication_state": "READY_FOR_OWNER_ACTION",
        "economics": None if policy is None else {
            "marketplace_fee_rate": str(policy.marketplace_fee_rate),
            "creator_share_rate": str(policy.creator_share_rate),
            "source": policy.source_url,
            "checked_at": policy.checked_at.isoformat(),
            "payout_rail": policy.payout_rail,
        },
        "api_source": {
            "openapi_url": "https://earn.amarktai.co.za/api/openapi.json",
            "base_url": "https://earn.amarktai.co.za",
            "source_contract": API_MARKET_PRODUCT_SOURCE,
        },
        "backend_auth": {
            "mode": "PRODUCT_SCOPED_AMARKTAI_BEARER_KEY",
            "header": "Authorization",
            "value_template": "Bearer <OWNER_ISSUED_PRODUCT_BACKEND_KEY>",
            "secret_in_export": False,
            "owner_command": "python manage.py issue_api_distribution_key --channel api-market --product <slug>",
        },
        "gateway_contract": {
            "buyer_identity_header": "x-magicapi-user",
            "plan_header": "x-magicapi-plan",
            "request_id_header": "x-request-id",
            "usage_response_header": "X-Magicapi-Billing",
            "submit_usage": "API=1;",
            "status_result_usage": "API=0;",
            "seller_enforces_rate_limit": True,
            "mcp_endpoint_generated_by_marketplace": True,
        },
        "products": [_product_row(product) for product in products],
        "activation_blockers": [
            "API_MARKET_SELLER_ACCOUNT_REQUIRED",
            "API_MARKET_OWNER_PUBLICATION_REQUIRED",
            "API_MARKET_PAYOUT_ROUTE_PROOF_REQUIRED",
            "SOUTH_AFRICA_PAYOUT_PROOF_REQUIRED",
            "PRODUCT_SCOPED_BACKEND_KEYS_REQUIRED",
        ],
    }


def zyla_export() -> dict[str, Any]:
    policy = active_channel_economics(ZYLA_CHANNEL)
    products = list(CommercialAPIProduct.objects.filter(enabled=True).prefetch_related("plans").order_by("slug"))
    launch_products = [product for product in products if product.slug in ZYLA_LAUNCH_PRODUCT_SLUGS]
    blocked_products = [
        {
            "slug": product.slug,
            "reason": "ASYNC_CALLER_IDENTITY_OR_RESULT_BILLING_CONTRACT_NOT_PROVEN",
        }
        for product in products
        if product.slug not in ZYLA_LAUNCH_PRODUCT_SLUGS
    ]
    return {
        "channel": ZYLA_CHANNEL,
        "role": "PAID_API_STOREFRONT",
        "published": False,
        "external_mutation_allowed": False,
        "connection_state": "READY_FOR_OWNER_ACTION",
        "publication_state": "READY_FOR_OWNER_ACTION",
        "economics": None if policy is None else {
            "admission_fee_reserve_rate": str(policy.marketplace_fee_rate),
            "creator_share_rate": str(policy.creator_share_rate),
            "source": policy.source_url,
            "checked_at": policy.checked_at.isoformat(),
            "payout_rail": policy.payout_rail,
            "metadata": policy.metadata,
        },
        "listing_contract": {
            "base_url": "https://earn.amarktai.co.za",
            "pricing_basis": "monthly request plans",
            "hard_limit_supported": True,
            "provider_uptime_matters_to_share": True,
            "backend_auth": "OWNER_CONFIGURED_PRODUCT_SCOPED_AMARKTAI_BEARER_KEY",
            "owner_command": "python manage.py issue_api_distribution_key --channel zyla-api-hub --product data-cleanup",
        },
        "products": [_product_row(product) for product in launch_products],
        "deferred_products": blocked_products,
        "activation_blockers": [
            "ZYLA_PROVIDER_ACCOUNT_REQUIRED",
            "ZYLA_OWNER_PUBLICATION_AND_QA_REVIEW_REQUIRED",
            "ZYLA_BACKEND_ACCESS_CONTROL_CONFIGURATION_REQUIRED",
            "ZYLA_PAYPAL_PAYOUT_PROOF_REQUIRED",
            "SOUTH_AFRICA_PAYPAL_RECEIPT_PROOF_REQUIRED",
        ],
    }


def postman_collection() -> dict[str, Any]:
    products = CommercialAPIProduct.objects.filter(enabled=True).order_by("slug")
    items: list[dict[str, Any]] = []
    for product in products:
        items.append({
            "name": product.display_name,
            "request": {
                "method": "POST",
                "header": [
                    {"key": "Content-Type", "value": "application/json"},
                    {"key": "Idempotency-Key", "value": "{{$guid}}"},
                ],
                "body": {"mode": "raw", "raw": json.dumps((product.examples or {}).get("request", {}), indent=2), "options": {"raw": {"language": "json"}}},
                "url": {"raw": f"{{{{baseUrl}}}}/api/v1/products/{product.slug}/jobs", "host": ["{{baseUrl}}"], "path": ["api", "v1", "products", product.slug, "jobs"]},
                "description": product.problem_statement,
            },
        })
    items.extend([
        {
            "name": "Get request status",
            "request": {
                "method": "GET",
                "url": {"raw": "{{baseUrl}}/api/v1/requests/{{requestId}}", "host": ["{{baseUrl}}"], "path": ["api", "v1", "requests", "{{requestId}}"]},
                "description": "Poll the canonical request state for an asynchronous commercial API job.",
            },
        },
        {
            "name": "Get QA-approved result",
            "request": {
                "method": "GET",
                "url": {"raw": "{{baseUrl}}/api/v1/requests/{{requestId}}/result", "host": ["{{baseUrl}}"], "path": ["api", "v1", "requests", "{{requestId}}", "result"]},
                "description": "Fetch a result only after canonical QA has passed.",
            },
        },
    ])
    return {
        "info": {
            "name": "AmarktAI Earn Commercial API",
            "description": "Canonical AmarktAI commercial API collection. Purchase/entitlement happens outside Postman; never place a real secret in a public collection.",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{amarktaiApiKey}}", "type": "string"}]},
        "variable": [
            {"key": "baseUrl", "value": "https://earn.amarktai.co.za", "type": "string"},
            {"key": "amarktaiApiKey", "value": "<SET_AFTER_PURCHASE_OR_OWNER_ENTITLEMENT>", "type": "string"},
            {"key": "requestId", "value": "<REQUEST_ID>", "type": "string"},
        ],
        "item": items,
    }


def postman_export() -> dict[str, Any]:
    collection = postman_collection()
    serialized = json.dumps(collection, sort_keys=True)
    return {
        "channel": POSTMAN_CHANNEL,
        "role": "DISCOVERY_AND_DEVELOPER_ONBOARDING",
        "revenue_source": False,
        "published": False,
        "external_mutation_allowed": False,
        "publication_state": "READY_FOR_OWNER_ACTION",
        "source": POSTMAN_SOURCE,
        "prepare_source": POSTMAN_PREPARE_SOURCE,
        "openapi_url": "https://earn.amarktai.co.za/api/openapi.json",
        "collection": collection,
        "contains_secret_material": "ak_" in serialized or "Bearer ak_" in serialized,
        "activation_blockers": [
            "POSTMAN_ACCOUNT_OR_TEAM_WORKSPACE_REQUIRED",
            "PUBLIC_WORKSPACE_OWNER_PUBLICATION_REQUIRED",
            "PUBLIC_COLLECTION_SECRET_SCAN_REQUIRED",
        ],
    }


def api_distribution_snapshot() -> dict[str, Any]:
    direct_products = list(CommercialAPIProduct.objects.filter(enabled=True).order_by("slug").values_list("slug", flat=True))
    rapid = {
        "channel": "rapidapi",
        "role": "PAID_API_STOREFRONT",
        "products": direct_products,
        "published": False,
        "publication_state": "READY_FOR_OWNER_ACTION",
    }
    apify = {
        "channel": "apify-store",
        "role": "PAID_ACTOR_AND_AGENT_STOREFRONT",
        "published": False,
        "publication_state": "READY_FOR_OWNER_ACTION",
    }
    return {
        "section": "commercial-api-distribution",
        "canonical_backend": "https://earn.amarktai.co.za/api/v1",
        "canonical_openapi": "https://earn.amarktai.co.za/api/openapi.json",
        "single_execution_engine": True,
        "single_product_catalog": True,
        "channels": {
            "direct": {"role": "DIRECT_PAID_API", "products": direct_products, "ready": True},
            "rapidapi": rapid,
            API_MARKET_CHANNEL: api_market_export(),
            ZYLA_CHANNEL: zyla_export(),
            POSTMAN_CHANNEL: postman_export(),
            "apify-store": apify,
        },
        "external_mutation_allowed": False,
    }


def api_distribution_acceptance_report() -> dict[str, Any]:
    snapshot = api_distribution_snapshot()
    api_market = snapshot["channels"][API_MARKET_CHANNEL]
    zyla = snapshot["channels"][ZYLA_CHANNEL]
    postman = snapshot["channels"][POSTMAN_CHANNEL]
    product_count = CommercialAPIProduct.objects.filter(enabled=True).count()

    criteria = [
        {"name": "CANONICAL_BACKEND", "status": "PASS" if snapshot["single_execution_engine"] and snapshot["single_product_catalog"] else "FAIL"},
        {"name": "API_MARKET_EXPORT", "status": "READY_FOR_OWNER_ACTION" if len(api_market.get("products", [])) == product_count and not api_market.get("published") else "FAIL"},
        {"name": "API_MARKET_ECONOMICS", "status": "PASS" if api_market.get("economics") and api_market["economics"].get("marketplace_fee_rate") == "0.200000" else "FAIL"},
        {"name": "ZYLA_SAFE_SCOPE", "status": "READY_FOR_OWNER_ACTION" if [row["slug"] for row in zyla.get("products", [])] == sorted(ZYLA_LAUNCH_PRODUCT_SLUGS) and not zyla.get("published") else "FAIL"},
        {"name": "POSTMAN_PACKAGE", "status": "READY_FOR_OWNER_ACTION" if len(postman.get("collection", {}).get("item", [])) >= product_count and not postman.get("contains_secret_material") else "FAIL"},
        {"name": "NO_EXTERNAL_PUBLICATION", "status": "PASS" if snapshot.get("external_mutation_allowed") is False and all(not row.get("published", False) for key, row in snapshot["channels"].items() if key != "direct") else "FAIL"},
    ]
    failures = [row for row in criteria if row["status"] == "FAIL"]
    return {
        "name": "API_DISTRIBUTION_ACCEPTANCE",
        "status": "FAIL" if failures else "PASS",
        "criteria": criteria,
        "summary": {
            "PASS": sum(row["status"] == "PASS" for row in criteria),
            "READY_FOR_OWNER_ACTION": sum(row["status"] == "READY_FOR_OWNER_ACTION" for row in criteria),
            "FAIL": len(failures),
        },
        "channels": list(snapshot["channels"]),
        "product_count": product_count,
        "external_mutations_performed": False,
    }
