from __future__ import annotations

import os
from collections import defaultdict
from decimal import Decimal

from control.services.channel_commercial import priority_channel_commercial_pricing_snapshot
from control.services.channel_packages import priority_channel_package_snapshot


EXPORT_CONTRACT_VERSION = 1


OFFICIAL_CONTRACT_REFERENCES = {
    "contra": [
        "https://help.contra.com/en/articles/13655628-payment-links",
        "https://help.contra.com/en/articles/13604374-digital-products",
        "https://help.contra.com/en/articles/9322763-paid-projects",
    ],
    "rapidapi": [
        "https://docs.rapidapi.com/v2.0.0/docs/additional-request-headers",
        "https://docs.rapidapi.com/v2.0.0/docs/configuring-api-security",
    ],
    "apify-store": [
        "https://docs.apify.com/actors/development/actor-definition/actor-json",
        "https://docs.apify.com/actors/development/actor-definition/input-schema/specification/v1",
        "https://docs.apify.com/actors/development/actor-definition/output-schema",
    ],
    "lemon-squeezy": [
        "https://docs.lemonsqueezy.com/help/webhooks/signing-requests",
        "https://docs.lemonsqueezy.com/guides/developer-guide/webhooks",
        "https://docs.lemonsqueezy.com/api",
    ],
}


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _package_input_schema(row: dict) -> dict:
    properties = {
        key: {
            "type": "string",
            "title": key.replace("_", " ").title(),
        }
        for key in row.get("buyer_inputs") or []
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _package_output_schema(row: dict) -> dict:
    deliverables = list(row.get("deliverables") or [])
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "artifacts": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Canonical Amarktai artifact references.",
            },
            "deliverables": {
                "type": "array",
                "items": {"type": "string"},
                "default": deliverables,
            },
        },
        "required": ["artifacts"],
    }


def _commercial_price_truth(row: dict, commercial_by_slug: dict[str, dict]) -> dict:
    commercial = commercial_by_slug.get(row["package_slug"]) or {}
    blockers = list(commercial.get("blockers") or [])
    price = commercial.get("public_price")
    if price in (None, ""):
        blockers.append("COMMERCIAL_PUBLIC_PRICE_NOT_SET")
    return {
        "currency": commercial.get("currency") or row.get("currency") or "USD",
        "internal_economic_floor": str(row.get("minimum_profitable_price") or row.get("shadow_price") or "0"),
        "internal_price_ready": bool(row.get("price_ready")),
        "public_price": price,
        "billing_unit": commercial.get("billing_unit"),
        "commercial_price_state": commercial.get("state") or "UNPREPARED",
        "owner_approved": bool(commercial.get("owner_approved")),
        "publication_pricing_blockers": list(dict.fromkeys(blockers)),
    }


def _common_export(row: dict, commercial_by_slug: dict[str, dict]) -> dict:
    pricing = _commercial_price_truth(row, commercial_by_slug)
    return {
        "contract_version": EXPORT_CONTRACT_VERSION,
        "market": row["market"],
        "package_slug": row["package_slug"],
        "display_name": row["display_name"],
        "description": row.get("sales_copy") or "",
        "operation": row["operation"],
        "pricing_model": row["pricing_model"],
        "execution_placement": row["execution_placement"],
        "buyer_inputs": list(row.get("buyer_inputs") or []),
        "deliverables": list(row.get("deliverables") or []),
        "input_schema": _package_input_schema(row),
        "output_schema": _package_output_schema(row),
        "pricing": pricing,
        "listing_status": row.get("listing_status"),
        "external_mutation_allowed": False,
        "publication_ready": False,
    }


def _contra_exports(rows: list[dict], commercial_by_slug: dict[str, dict]) -> list[dict]:
    exports = []
    for row in rows:
        payload = _common_export(row, commercial_by_slug)
        price_blockers = list(payload["pricing"]["publication_pricing_blockers"])
        payload.update({
            "export_type": "CONTRA_MANUAL_OFFER",
            "publication_mode": "OWNER_MANUAL",
            "supported_surfaces": ["PROJECT", "PAYMENT_LINK", "DIGITAL_PRODUCT"],
            "manual_fields": {
                "title": row["display_name"],
                "description": row.get("sales_copy") or "",
                "deliverables": list(row.get("deliverables") or []),
                "price": payload["pricing"]["public_price"],
                "currency": payload["pricing"]["currency"],
            },
            "inbound_contract": {
                "mode": "OWNER_MANUAL_IMPORT_PLUS_SIGNED_BUYER_INTAKE",
                "import_path": "/api/channels/contra/orders",
                "external_mutation_performed": False,
            },
            "publication_blockers": list(dict.fromkeys([
                *price_blockers,
                "PUBLIC_AUTOMATION_CONTRACT_NOT_VERIFIED",
                "OWNER_ACCOUNT_AND_PAYOUT_PROOF_REQUIRED",
            ])),
        })
        exports.append(payload)
    return exports


def _rapidapi_accepted_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "accepted": {"type": "boolean"},
            "created": {"type": "boolean"},
            "order_id": {"type": "string", "format": "uuid"},
            "status_path": {"type": "string"},
            "order_status": {"type": "string"},
            "job_state": {"type": "string"},
        },
        "required": ["accepted", "order_id", "status_path"],
    }


def _rapidapi_status_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "format": "uuid"},
            "package_slug": {"type": "string"},
            "order_status": {"type": "string"},
            "remote_state": {"type": "string"},
            "job_state": {"type": "string"},
            "workplan_state": {"type": ["string", "null"]},
            "artifacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "artifact_id": {"type": "integer"},
                        "mime_type": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "download_path": {"type": "string"},
                    },
                },
            },
        },
    }


def _rapidapi_export(rows: list[dict], commercial_by_slug: dict[str, dict]) -> dict:
    paths = {}
    packages = []
    for row in rows:
        package = _common_export(row, commercial_by_slug)
        packages.append(package)
        schema = _package_input_schema(row)
        schema["properties"] = {
            "request_id": {
                "type": "string",
                "title": "Request Id",
                "description": "Caller-supplied idempotency identifier for this API operation.",
            },
            **schema["properties"],
        }
        schema["required"] = ["request_id", *schema["required"]]
        paths[f"/{row['package_slug']}"] = {
            "post": {
                "operationId": row["package_slug"].replace("-", "_"),
                "summary": row["display_name"],
                "description": (row.get("sales_copy") or "") + " Requests are accepted asynchronously; poll the returned status path for QA-verified artifacts.",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": schema}},
                },
                "responses": {
                    "202": {
                        "description": "Request authenticated, persisted and queued for canonical execution.",
                        "content": {"application/json": {"schema": _rapidapi_accepted_schema()}},
                    },
                    "400": {"description": "Invalid request payload."},
                    "401": {"description": "Rapid proxy authentication failed."},
                    "409": {"description": "Idempotency or order-state conflict."},
                    "503": {"description": "Package or provider ingress is not activated."},
                },
            }
        }
    paths["/orders/{order_id}"] = {
        "get": {
            "operationId": "get_order_status",
            "summary": "Get asynchronous order status and QA-verified artifact references",
            "parameters": [{"name": "order_id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}],
            "responses": {
                "200": {"description": "Canonical order status.", "content": {"application/json": {"schema": _rapidapi_status_schema()}}},
                "401": {"description": "Rapid proxy authentication failed."},
                "403": {"description": "Order does not belong to the RapidAPI user."},
                "404": {"description": "Order not found."},
            },
        }
    }
    paths["/orders/{order_id}/artifacts/{artifact_id}"] = {
        "get": {
            "operationId": "download_order_artifact",
            "summary": "Download a QA-verified result artifact",
            "parameters": [
                {"name": "order_id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}},
                {"name": "artifact_id", "in": "path", "required": True, "schema": {"type": "integer"}},
            ],
            "responses": {
                "200": {"description": "QA-verified result artifact."},
                "401": {"description": "Rapid proxy authentication failed."},
                "403": {"description": "Order does not belong to the RapidAPI user."},
                "404": {"description": "Artifact not found."},
                "409": {"description": "Result is not QA-ready yet."},
            },
        }
    }
    secret_configured = bool(os.getenv("RAPIDAPI_PROXY_SECRET", "").strip())
    ingress_enabled = _truthy_env("RAPIDAPI_PUBLIC_INGRESS_ENABLED") and secret_configured
    price_blockers = sorted({
        blocker
        for package in packages
        for blocker in package["pricing"]["publication_pricing_blockers"]
    })
    return {
        "contract_version": EXPORT_CONTRACT_VERSION,
        "market": "rapidapi",
        "export_type": "RAPIDAPI_PROVIDER_OPENAPI_DRAFT",
        "openapi": {
            "openapi": "3.0.3",
            "info": {
                "title": "Amarktai Deterministic Utility APIs",
                "version": "1.0.0-draft",
                "description": "Local provider export only. No RapidAPI listing or public ingress is activated by this document.",
            },
            "servers": [{"url": "https://earn.amarktai.co.za/api/channels/rapidapi"}],
            "paths": paths,
        },
        "provider_ingress_contract": {
            "mode": "RAPID_PROXY_SECRET",
            "required_proxy_header": "X-RapidAPI-Proxy-Secret",
            "required_buyer_header": "X-RapidAPI-User",
            "comparison": "CONSTANT_TIME",
            "secret_configured": secret_configured,
            "ingress_enabled": ingress_enabled,
            "execution_mode": "ASYNC_202_POLL_ARTIFACT",
            "public_endpoint_state": "CONFIGURED_BUT_FAIL_CLOSED" if secret_configured else "NOT_CONFIGURED",
        },
        "packages": packages,
        "publication_blockers": list(dict.fromkeys([
            *price_blockers,
            "PROVIDER_ACCOUNT_NOT_CONFIGURED",
            "PUBLIC_INGRESS_NOT_ACTIVATED",
            "PAYPAL_AND_SOUTH_AFRICA_WITHDRAWAL_NOT_VERIFIED",
        ])),
        "external_mutation_allowed": False,
        "publication_ready": False,
    }


def _apify_export(rows: list[dict], commercial_by_slug: dict[str, dict]) -> dict:
    row = rows[0] if rows else None
    if row is None:
        return {
            "contract_version": EXPORT_CONTRACT_VERSION,
            "market": "apify-store",
            "export_type": "APIFY_ACTOR_BUNDLE_DRAFT",
            "package_missing": True,
            "external_mutation_allowed": False,
            "publication_ready": False,
        }
    input_schema = _package_input_schema(row)
    output_schema = {
        "actorOutputSchemaVersion": 1,
        "title": "Amarktai Website Data Extractor output",
        "properties": {
            "results": {
                "type": "string",
                "title": "Dataset results",
                "template": "{{links.apiDefaultDatasetUrl}}/items",
            }
        },
    }
    package = _common_export(row, commercial_by_slug)
    return {
        "contract_version": EXPORT_CONTRACT_VERSION,
        "market": "apify-store",
        "export_type": "APIFY_ACTOR_BUNDLE_DRAFT",
        "actor_files": {
            ".actor/actor.json": {
                "actorSpecification": 1,
                "name": "amarktai-website-data-extractor",
                "title": row["display_name"],
                "version": "0.1",
                "dockerfile": "./Dockerfile",
                "input": "./input_schema.json",
                "output": "./output_schema.json",
            },
            ".actor/input_schema.json": {
                "title": row["display_name"],
                "description": row.get("sales_copy") or "",
                "type": "object",
                "schemaVersion": 1,
                "properties": input_schema["properties"],
                "required": input_schema["required"],
            },
            ".actor/output_schema.json": output_schema,
        },
        "repository_bundle": {
            "root": "integrations/apify_actor",
            "actor_definition": "integrations/apify_actor/.actor/actor.json",
            "runtime": "integrations/apify_actor/src/main.py",
            "dockerfile": "integrations/apify_actor/.actor/Dockerfile",
            "source_state": "REPOSITORY_BUNDLE_PREPARED",
        },
        "package": package,
        "execution_contract": {
            "placement": "APIFY",
            "continuous_scraping_on_webdock_allowed": False,
            "actor_runtime_source_state": "REPOSITORY_BUNDLE_PREPARED",
        },
        "publication_blockers": list(dict.fromkeys([
            *package["pricing"]["publication_pricing_blockers"],
            "EXTERNAL_EXECUTION_COST_PROFILE_NOT_PROVEN",
            "CREATOR_ACCOUNT_KYC_AND_PAYOUT_NOT_VERIFIED",
        ])),
        "external_mutation_allowed": False,
        "publication_ready": False,
    }


def _lemon_export(rows: list[dict], commercial_by_slug: dict[str, dict]) -> dict:
    secret_configured = bool(os.getenv("LEMON_SQUEEZY_WEBHOOK_SECRET", "").strip())
    webhook_enabled = _truthy_env("LEMON_SQUEEZY_WEBHOOK_ENABLED") and secret_configured
    products = []
    for row in rows:
        common = _common_export(row, commercial_by_slug)
        products.append({
            **common,
            "export_type": "LEMON_PRODUCT_DRAFT",
            "checkout_mode": "SUBSCRIPTION" if row["pricing_model"] == "SUBSCRIPTION" else "ONE_TIME",
            "variant_pricing": {
                "amount": common["pricing"]["public_price"],
                "currency": common["pricing"]["currency"],
                "billing_unit": common["pricing"]["billing_unit"],
            } if common["pricing"]["public_price"] else None,
        })
    price_blockers = sorted({
        blocker
        for product in products
        for blocker in product["pricing"]["publication_pricing_blockers"]
    })
    return {
        "contract_version": EXPORT_CONTRACT_VERSION,
        "market": "lemon-squeezy",
        "export_type": "LEMON_STORE_CATALOG_DRAFT",
        "products": products,
        "webhook_contract": {
            "method": "POST",
            "path": "/webhooks/lemon-squeezy/",
            "signature_header": "X-Signature",
            "signature_algorithm": "HMAC-SHA256",
            "secret_configured": secret_configured,
            "receiver_enabled": webhook_enabled,
            "receiver_state": "CONFIGURED_BUT_FAIL_CLOSED" if secret_configured else "NOT_CONFIGURED",
            "buyer_intake": "SIGNED_EXPIRING_ORDER_LINK",
            "recommended_events": [
                "order_created",
                "subscription_created",
                "subscription_updated",
                "subscription_expired",
            ],
        },
        "publication_blockers": list(dict.fromkeys([
            *price_blockers,
            "MERCHANT_ACCOUNT_KYC_AND_BANK_PAYOUT_NOT_VERIFIED",
            "SIGNED_WEBHOOK_RECEIVER_NOT_ACTIVATED",
        ])),
        "external_mutation_allowed": False,
        "publication_ready": False,
    }


def priority_channel_export_snapshot() -> dict:
    packages = priority_channel_package_snapshot()
    commercial = priority_channel_commercial_pricing_snapshot()
    commercial_by_slug = {row["package_slug"]: row for row in commercial["rows"]}
    by_market: dict[str, list[dict]] = defaultdict(list)
    for row in packages["rows"]:
        by_market[row["market"]].append(row)

    contra = _contra_exports(by_market.get("contra", []), commercial_by_slug)
    rapidapi = _rapidapi_export(by_market.get("rapidapi", []), commercial_by_slug)
    apify = _apify_export(by_market.get("apify-store", []), commercial_by_slug)
    lemon = _lemon_export(by_market.get("lemon-squeezy", []), commercial_by_slug)

    export_rows = [*contra, rapidapi, apify, lemon]
    package_exports = len(contra) + len(rapidapi.get("packages") or []) + int(bool(apify.get("package"))) + len(lemon.get("products") or [])
    priced = sum(1 for row in commercial["rows"] if row["prepared"])
    return {
        "section": "priority-channel-publication-exports",
        "rows": export_rows,
        "meta": {
            "contract_version": EXPORT_CONTRACT_VERSION,
            "package_exports": package_exports,
            "source_packages": packages["meta"]["total_packages"],
            "public_prices_set": priced,
            "publication_ready_packages": 0,
            "external_mutation_allowed": False,
            "official_contract_references": OFFICIAL_CONTRACT_REFERENCES,
            "truth": "Exports are local machine-readable preparation. Commercial price proposals are distinct from internal economic floors. No account, listing, API, Actor, product, checkout, webhook, or public ingress is created or activated by this snapshot.",
        },
    }
