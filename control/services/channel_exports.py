from __future__ import annotations

import os
from collections import defaultdict
from decimal import Decimal

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


def _commercial_price_truth(row: dict) -> dict:
    internal = _decimal(row.get("shadow_price"))
    blockers = list(row.get("pricing_blockers") or [])
    blockers.append("COMMERCIAL_PUBLIC_PRICE_NOT_SET")
    return {
        "currency": row.get("currency") or "USD",
        "internal_economic_floor": str(internal),
        "internal_price_ready": bool(row.get("price_ready")),
        "public_price": None,
        "commercial_price_state": "OWNER_COMMERCIAL_PRICING_REQUIRED",
        "publication_pricing_blockers": list(dict.fromkeys(blockers)),
    }


def _common_export(row: dict) -> dict:
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
        "pricing": _commercial_price_truth(row),
        "listing_status": row.get("listing_status"),
        "external_mutation_allowed": False,
        "publication_ready": False,
    }


def _contra_exports(rows: list[dict]) -> list[dict]:
    exports = []
    for row in rows:
        payload = _common_export(row)
        payload.update({
            "export_type": "CONTRA_MANUAL_OFFER",
            "publication_mode": "OWNER_MANUAL",
            "supported_surfaces": ["PROJECT", "PAYMENT_LINK", "DIGITAL_PRODUCT"],
            "manual_fields": {
                "title": row["display_name"],
                "description": row.get("sales_copy") or "",
                "deliverables": list(row.get("deliverables") or []),
            },
            "publication_blockers": [
                "PUBLIC_AUTOMATION_CONTRACT_NOT_VERIFIED",
                "COMMERCIAL_PUBLIC_PRICE_NOT_SET",
                "OWNER_ACCOUNT_AND_PAYOUT_PROOF_REQUIRED",
            ],
        })
        exports.append(payload)
    return exports


def _rapidapi_export(rows: list[dict]) -> dict:
    paths = {}
    for row in rows:
        paths[f"/{row['package_slug']}"] = {
            "post": {
                "operationId": row["package_slug"].replace("-", "_"),
                "summary": row["display_name"],
                "description": row.get("sales_copy") or "",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": _package_input_schema(row),
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Successful canonical Amarktai response.",
                        "content": {
                            "application/json": {
                                "schema": _package_output_schema(row),
                            }
                        },
                    },
                    "401": {"description": "Rapid proxy authentication failed."},
                    "503": {"description": "Package or provider ingress is not activated."},
                },
            }
        }
    secret_configured = bool(os.getenv("RAPIDAPI_PROXY_SECRET", "").strip())
    ingress_enabled = _truthy_env("RAPIDAPI_PUBLIC_INGRESS_ENABLED") and secret_configured
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
            "paths": paths,
        },
        "provider_ingress_contract": {
            "mode": "RAPID_PROXY_SECRET",
            "required_header": "X-RapidAPI-Proxy-Secret",
            "comparison": "CONSTANT_TIME",
            "secret_configured": secret_configured,
            "ingress_enabled": ingress_enabled,
            "public_endpoint_state": "NOT_WIRED_OR_ACTIVATED",
        },
        "packages": [_common_export(row) for row in rows],
        "publication_blockers": [
            "COMMERCIAL_TIER_PRICING_REQUIRED",
            "PROVIDER_ACCOUNT_NOT_CONFIGURED",
            "PUBLIC_INGRESS_NOT_ACTIVATED",
            "PAYPAL_AND_SOUTH_AFRICA_WITHDRAWAL_NOT_VERIFIED",
        ],
        "external_mutation_allowed": False,
        "publication_ready": False,
    }


def _apify_export(rows: list[dict]) -> dict:
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
    return {
        "contract_version": EXPORT_CONTRACT_VERSION,
        "market": "apify-store",
        "export_type": "APIFY_ACTOR_BUNDLE_DRAFT",
        "actor_files": {
            ".actor/actor.json": {
                "actorSpecification": 1,
                "name": "amarktai-website-data-extractor",
                "title": row["display_name"],
                "version": "0.1.0",
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
        "package": _common_export(row),
        "execution_contract": {
            "placement": "APIFY",
            "continuous_scraping_on_webdock_allowed": False,
            "actor_runtime_source_state": "NOT_EXPORTED_YET",
        },
        "publication_blockers": [
            "EXTERNAL_EXECUTION_COST_PROFILE_NOT_PROVEN",
            "ACTOR_RUNTIME_SOURCE_NOT_EXPORTED",
            "CREATOR_ACCOUNT_KYC_AND_PAYOUT_NOT_VERIFIED",
            "COMMERCIAL_PUBLIC_PRICE_NOT_SET",
        ],
        "external_mutation_allowed": False,
        "publication_ready": False,
    }


def _lemon_export(rows: list[dict]) -> dict:
    secret_configured = bool(os.getenv("LEMON_SQUEEZY_WEBHOOK_SECRET", "").strip())
    webhook_enabled = _truthy_env("LEMON_SQUEEZY_WEBHOOK_ENABLED") and secret_configured
    products = []
    for row in rows:
        common = _common_export(row)
        products.append({
            **common,
            "export_type": "LEMON_PRODUCT_DRAFT",
            "checkout_mode": "SUBSCRIPTION" if row["pricing_model"] == "SUBSCRIPTION" else "ONE_TIME",
            "variant_pricing": None,
        })
    return {
        "contract_version": EXPORT_CONTRACT_VERSION,
        "market": "lemon-squeezy",
        "export_type": "LEMON_STORE_CATALOG_DRAFT",
        "products": products,
        "webhook_contract": {
            "method": "POST",
            "signature_header": "X-Signature",
            "signature_algorithm": "HMAC-SHA256",
            "secret_configured": secret_configured,
            "receiver_enabled": webhook_enabled,
            "receiver_state": "NOT_WIRED_OR_ACTIVATED",
            "recommended_events": [
                "order_created",
                "subscription_created",
                "subscription_updated",
                "subscription_expired",
            ],
        },
        "publication_blockers": [
            "COMMERCIAL_PRODUCT_PRICING_REQUIRED",
            "MERCHANT_ACCOUNT_KYC_AND_BANK_PAYOUT_NOT_VERIFIED",
            "SIGNED_WEBHOOK_RECEIVER_NOT_ACTIVATED",
        ],
        "external_mutation_allowed": False,
        "publication_ready": False,
    }


def priority_channel_export_snapshot() -> dict:
    packages = priority_channel_package_snapshot()
    by_market: dict[str, list[dict]] = defaultdict(list)
    for row in packages["rows"]:
        by_market[row["market"]].append(row)

    contra = _contra_exports(by_market.get("contra", []))
    rapidapi = _rapidapi_export(by_market.get("rapidapi", []))
    apify = _apify_export(by_market.get("apify-store", []))
    lemon = _lemon_export(by_market.get("lemon-squeezy", []))

    export_rows = [*contra, rapidapi, apify, lemon]
    package_exports = len(contra) + len(rapidapi.get("packages") or []) + int(bool(apify.get("package"))) + len(lemon.get("products") or [])
    return {
        "section": "priority-channel-publication-exports",
        "rows": export_rows,
        "meta": {
            "contract_version": EXPORT_CONTRACT_VERSION,
            "package_exports": package_exports,
            "source_packages": packages["meta"]["total_packages"],
            "public_prices_set": 0,
            "publication_ready_packages": 0,
            "external_mutation_allowed": False,
            "official_contract_references": OFFICIAL_CONTRACT_REFERENCES,
            "truth": "Exports are local machine-readable preparation. Internal economic floors are not public customer prices. No account, listing, API, Actor, product, checkout, webhook, or public ingress is created or activated by this snapshot.",
        },
    }
