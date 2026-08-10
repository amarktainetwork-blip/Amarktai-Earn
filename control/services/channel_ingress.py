from __future__ import annotations

import base64
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import Artifact, AuditEvent, InboundOrder, MarketServiceListing, Marketplace, ServiceOffering
from control.services.autonomy import AutonomyMode, current_mode
from control.services.seller_services import receive_inbound_order, run_inbound_economic_preflight
from planning.models import WorkPlan
from planning.services import stage_local_job_asset


CENT = Decimal("0.01")
RAPIDAPI_MARKET = "rapidapi"
LEMON_MARKET = "lemon-squeezy"
CONTRA_MARKET = "contra"


class ChannelIngressError(ValueError):
    def __init__(self, code: str, *, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _commercial_record(listing: MarketServiceListing) -> dict:
    metadata = listing.platform_metadata if isinstance(listing.platform_metadata, dict) else {}
    record = metadata.get("commercial_pricing")
    return record if isinstance(record, dict) else {}


def _package_manifest(listing: MarketServiceListing) -> dict:
    metadata = listing.platform_metadata if isinstance(listing.platform_metadata, dict) else {}
    record = metadata.get("channel_package")
    return record if isinstance(record, dict) else {}


def _listing(package_slug: str, *, market_slug: str | None = None) -> MarketServiceListing:
    query = MarketServiceListing.objects.select_related("offering", "marketplace", "marketplace__integration_profile")
    try:
        listing = query.get(offering__slug=package_slug)
    except MarketServiceListing.DoesNotExist as exc:
        raise ChannelIngressError("UNKNOWN_CHANNEL_PACKAGE", status=404) from exc
    if market_slug and listing.marketplace.slug != market_slug:
        raise ChannelIngressError("CHANNEL_PACKAGE_MARKET_MISMATCH", status=404)
    return listing


def _activation_blockers(listing: MarketServiceListing) -> list[str]:
    market = listing.marketplace
    offering = listing.offering
    blockers: list[str] = []
    if not market.enabled:
        blockers.append("MARKET_DISABLED")
    if market.status != Marketplace.Status.LIVE:
        blockers.append("MARKET_NOT_LIVE")
    if not market.payout_ready:
        blockers.append("PAYOUT_NOT_READY")
    if not market.south_africa_verified:
        blockers.append("SOUTH_AFRICA_PAYOUT_NOT_VERIFIED")
    if listing.status != MarketServiceListing.Status.PUBLISHED:
        blockers.append("SERVICE_LISTING_NOT_PUBLISHED")
    if not listing.remote_listing_id or not listing.remote_reference:
        blockers.append("REMOTE_PUBLICATION_EVIDENCE_REQUIRED")
    if not offering.enabled:
        blockers.append("SERVICE_OFFERING_DISABLED")
    if not offering.accepting_orders:
        blockers.append("SERVICE_NOT_ACCEPTING_ORDERS")
    commercial = _commercial_record(listing)
    if not commercial.get("public_price") or commercial.get("blockers"):
        blockers.append("COMMERCIAL_PUBLIC_PRICE_NOT_READY")
    try:
        profile = market.integration_profile
    except Exception:
        profile = None
    if profile is None or not profile.seller_capabilities.get("receive_orders"):
        blockers.append("RECEIVE_ORDERS_CAPABILITY_NOT_VERIFIED")
    return list(dict.fromkeys(blockers))


def _public_price(listing: MarketServiceListing) -> Decimal:
    record = _commercial_record(listing)
    price = _money(_decimal(record.get("public_price")))
    if price <= 0:
        raise ChannelIngressError("COMMERCIAL_PUBLIC_PRICE_NOT_READY", status=503)
    if price < _money(_decimal(listing.offering.minimum_profitable_price)):
        raise ChannelIngressError("COMMERCIAL_PUBLIC_PRICE_BELOW_PROFITABLE_FLOOR", status=503)
    return price


def _platform_fee(listing: MarketServiceListing, price: Decimal) -> Decimal:
    rate = _decimal(listing.offering.platform_fee_rate)
    if rate < 0 or rate >= 1:
        raise ChannelIngressError("MARKETPLACE_FEE_RATE_INVALID", status=503)
    return _money(price * rate)


def _require_public_runtime(listing: MarketServiceListing, *, flag_name: str, secret_name: str | None = None) -> None:
    blockers = _activation_blockers(listing)
    if not _truthy_env(flag_name):
        blockers.append(f"{flag_name}_DISABLED")
    if secret_name and not os.getenv(secret_name, "").strip():
        blockers.append(f"{secret_name}_NOT_CONFIGURED")
    if current_mode() not in {AutonomyMode.LOW_RISK, AutonomyMode.FULL}:
        blockers.append("PUBLIC_ORDER_INGRESS_REQUIRES_LOW_RISK_OR_FULL")
    if os.getenv("INBOUND_SERVICE_AUTO_ACCEPT_ENABLED", "0") != "1":
        blockers.append("INBOUND_SERVICE_AUTO_ACCEPT_DISABLED")
    if blockers:
        raise ChannelIngressError("CHANNEL_INGRESS_BLOCKED:" + ",".join(list(dict.fromkeys(blockers))), status=503)


def _constant_time_secret(candidate: str, *, env_name: str) -> bool:
    expected = os.getenv(env_name, "")
    return bool(expected and candidate and hmac.compare_digest(candidate.encode(), expected.encode()))


def _safe_job_source_dir(order: InboundOrder) -> Path:
    root = Path(os.getenv("AMARKTAI_JOB_ROOT", "/var/lib/amarktai-earn/jobs")).resolve()
    target = (root / str(order.job_id) / "inbound-source").resolve()
    if target != root and root not in target.parents:
        raise ChannelIngressError("INBOUND_SOURCE_PATH_ESCAPE", status=500)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _bounded_bytes(data: bytes) -> bytes:
    limit = max(1024, int(os.getenv("INBOUND_INLINE_ASSET_MAX_BYTES", str(10 * 1024 * 1024))))
    if len(data) > limit:
        raise ChannelIngressError("INBOUND_INLINE_ASSET_TOO_LARGE", status=413)
    return data


def _decode_image(value: Any) -> tuple[bytes, str]:
    text = str(value or "").strip()
    if not text:
        raise ChannelIngressError("RAPIDAPI_IMAGE_REQUIRED")
    if text.startswith("data:"):
        try:
            header, text = text.split(",", 1)
        except ValueError as exc:
            raise ChannelIngressError("RAPIDAPI_IMAGE_DATA_URL_INVALID") from exc
        if ";base64" not in header:
            raise ChannelIngressError("RAPIDAPI_IMAGE_MUST_BE_BASE64")
    try:
        raw = _bounded_bytes(base64.b64decode(text, validate=True))
    except Exception as exc:
        raise ChannelIngressError("RAPIDAPI_IMAGE_BASE64_INVALID") from exc
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return raw, ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return raw, ".jpg"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return raw, ".webp"
    raise ChannelIngressError("RAPIDAPI_IMAGE_FORMAT_UNSUPPORTED")


def _rapidapi_requirements_and_asset(listing: MarketServiceListing, body: dict) -> tuple[dict, tuple[str, bytes] | None]:
    operation = listing.offering.operation
    requirements = {key: value for key, value in body.items() if key != "request_id"}
    asset: tuple[str, bytes] | None = None

    if operation == "json_to_csv":
        raw = requirements.pop("json_payload", None)
        if raw is None:
            raise ChannelIngressError("RAPIDAPI_JSON_PAYLOAD_REQUIRED")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            payload = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode()
        except Exception as exc:
            raise ChannelIngressError("RAPIDAPI_JSON_PAYLOAD_INVALID") from exc
        asset = ("source.json", _bounded_bytes(payload))
        requirements["json_payload_staged"] = True
    elif operation == "csv_normalize":
        raw = requirements.pop("csv_input", None)
        if not isinstance(raw, str) or not raw.strip():
            raise ChannelIngressError("RAPIDAPI_CSV_INPUT_REQUIRED")
        asset = ("source.csv", _bounded_bytes(raw.encode("utf-8")))
        requirements["csv_input_staged"] = True
    elif operation in {"tabular_convert", "tabular_normalize"}:
        raw = requirements.pop("tabular_input", None)
        if raw in (None, ""):
            raise ChannelIngressError("RAPIDAPI_TABULAR_INPUT_REQUIRED")
        input_format = str(requirements.pop("input_format", "") or "").strip().lower()
        if not input_format:
            input_format = "json" if isinstance(raw, (dict, list)) or str(raw).lstrip().startswith(("{", "[")) else "csv"
        if input_format == "json":
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                data = json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode()
            except Exception as exc:
                raise ChannelIngressError("RAPIDAPI_TABULAR_JSON_INVALID") from exc
            asset = ("source.json", _bounded_bytes(data))
        elif input_format == "csv":
            if not isinstance(raw, str):
                raise ChannelIngressError("RAPIDAPI_TABULAR_CSV_INVALID")
            asset = ("source.csv", _bounded_bytes(raw.encode("utf-8")))
        else:
            raise ChannelIngressError("RAPIDAPI_TABULAR_INPUT_FORMAT_UNSUPPORTED")
        requirements["input_format"] = input_format
        requirements["tabular_input_staged"] = True
    elif operation in {"image_resize", "image_convert"}:
        raw = requirements.pop("image", None)
        image, suffix = _decode_image(raw)
        asset = (f"source{suffix}", image)
        requirements["image_staged"] = True

    return requirements, asset


def _attach_inline_asset(order: InboundOrder, asset: tuple[str, bytes] | None) -> None:
    if asset is None:
        return
    name, data = asset
    target = _safe_job_source_dir(order) / name
    target.write_bytes(data)
    try:
        staged = stage_local_job_asset(
            job_id=order.job_id,
            path=str(target),
            source=f"inbound:{order.marketplace.slug}",
            external_id=f"inbound:{order.id}:source",
            semantic_role="source",
        )
    except Exception:
        target.unlink(missing_ok=True)
        raise
    order.input_assets = [{
        "asset_id": str(staged.id),
        "sha256": staged.sha256,
        "size_bytes": staged.size_bytes,
        "semantic_role": staged.semantic_role,
    }]
    order.save(update_fields=["input_assets", "updated_at"])


def _enrich_job_inputs(order: InboundOrder) -> None:
    job = order.job
    payload = dict(job.normalized_payload or {})
    payload["inputs"] = dict(order.requirements or {})
    payload["requirements"] = dict(order.requirements or {})
    job.normalized_payload = payload
    job.save(update_fields=["normalized_payload", "updated_at"])


@transaction.atomic
def receive_rapidapi_call(*, package_slug: str, body: dict, proxy_secret: str, rapid_user: str) -> tuple[InboundOrder, bool]:
    if not _constant_time_secret(proxy_secret, env_name="RAPIDAPI_PROXY_SECRET"):
        raise ChannelIngressError("RAPIDAPI_PROXY_AUTHENTICATION_FAILED", status=401)
    listing = _listing(package_slug, market_slug=RAPIDAPI_MARKET)
    _require_public_runtime(listing, flag_name="RAPIDAPI_PUBLIC_INGRESS_ENABLED", secret_name="RAPIDAPI_PROXY_SECRET")
    if not isinstance(body, dict):
        raise ChannelIngressError("RAPIDAPI_BODY_INVALID")
    request_id = str(body.get("request_id") or "").strip()
    if not request_id or len(request_id) > 160:
        raise ChannelIngressError("RAPIDAPI_REQUEST_ID_REQUIRED")
    buyer = str(rapid_user or "").strip()[:255]
    if not buyer:
        raise ChannelIngressError("RAPIDAPI_USER_HEADER_REQUIRED", status=401)

    requirements, asset = _rapidapi_requirements_and_asset(listing, body)
    price = _public_price(listing)
    fee = _platform_fee(listing, price)
    identity = hashlib.sha256(f"{buyer}|{package_slug}|{request_id}".encode()).hexdigest()
    order, created = receive_inbound_order(
        marketplace=listing.marketplace,
        listing=listing,
        remote_order_id=f"rapidapi:{identity}",
        idempotency_key=f"rapidapi:{identity}",
        payload={
            "buyer_reference": buyer,
            "requirements": requirements,
            "input_assets": [],
            "quoted_price": str(price),
            "platform_fee": str(fee),
            "currency": listing.currency,
            "funding_state": "AUTHORIZED",
            "remote_state": "RECEIVED",
            "acceptance_probability": "0.99",
        },
        authenticated_market_identity=True,
        authenticated_at=timezone.now(),
    )
    if created:
        _attach_inline_asset(order, asset)
        order.refresh_from_db()
        _enrich_job_inputs(order)
    return order, created


def _rapidapi_authorized_order(order_id, *, proxy_secret: str, rapid_user: str) -> InboundOrder:
    if not _constant_time_secret(proxy_secret, env_name="RAPIDAPI_PROXY_SECRET"):
        raise ChannelIngressError("RAPIDAPI_PROXY_AUTHENTICATION_FAILED", status=401)
    try:
        order = InboundOrder.objects.select_related("job", "listing__offering", "marketplace").get(pk=order_id, marketplace__slug=RAPIDAPI_MARKET)
    except InboundOrder.DoesNotExist as exc:
        raise ChannelIngressError("RAPIDAPI_ORDER_NOT_FOUND", status=404) from exc
    if not rapid_user or not hmac.compare_digest(str(order.buyer_reference).encode(), str(rapid_user).encode()):
        raise ChannelIngressError("RAPIDAPI_ORDER_BUYER_MISMATCH", status=403)
    return order


def rapidapi_order_snapshot(order_id, *, proxy_secret: str, rapid_user: str) -> dict:
    order = _rapidapi_authorized_order(order_id, proxy_secret=proxy_secret, rapid_user=rapid_user)
    plan = WorkPlan.objects.filter(job_id=order.job_id).order_by("-updated_at").first()
    artifacts = Artifact.objects.filter(job_id=order.job_id).order_by("id")
    return {
        "order_id": str(order.id),
        "package_slug": order.listing.offering.slug if order.listing_id else None,
        "order_status": order.status,
        "remote_state": order.remote_state,
        "job_state": order.job.state,
        "workplan_state": plan.status if plan else None,
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
        "artifacts": [
            {
                "artifact_id": row.id,
                "mime_type": row.mime_type,
                "size_bytes": row.size_bytes,
                "download_path": f"/api/channels/rapidapi/orders/{order.id}/artifacts/{row.id}",
            }
            for row in artifacts
            if plan and plan.status in {WorkPlan.Status.QA_PASSED, WorkPlan.Status.SUBMITTED}
        ],
    }


def rapidapi_order_artifact(order_id, artifact_id: int, *, proxy_secret: str, rapid_user: str) -> Artifact:
    order = _rapidapi_authorized_order(order_id, proxy_secret=proxy_secret, rapid_user=rapid_user)
    plan = WorkPlan.objects.filter(job_id=order.job_id).order_by("-updated_at").first()
    if not plan or plan.status not in {WorkPlan.Status.QA_PASSED, WorkPlan.Status.SUBMITTED}:
        raise ChannelIngressError("RAPIDAPI_RESULT_NOT_READY", status=409)
    try:
        return Artifact.objects.get(pk=artifact_id, job_id=order.job_id)
    except Artifact.DoesNotExist as exc:
        raise ChannelIngressError("RAPIDAPI_ARTIFACT_NOT_FOUND", status=404) from exc


def verify_lemon_signature(raw_body: bytes, signature: str) -> bool:
    secret = os.getenv("LEMON_SQUEEZY_WEBHOOK_SECRET", "")
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), str(signature).strip().encode())


def _lemon_package_slug(payload: dict) -> str:
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    custom = meta.get("custom_data") if isinstance(meta.get("custom_data"), dict) else {}
    package_slug = str(custom.get("package_slug") or "").strip()
    if package_slug:
        return package_slug
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    attributes = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    variant_id = str(attributes.get("first_order_item", {}).get("variant_id") or "") if isinstance(attributes.get("first_order_item"), dict) else ""
    if variant_id:
        listing = MarketServiceListing.objects.filter(marketplace__slug=LEMON_MARKET, remote_listing_id=variant_id).select_related("offering").first()
        if listing:
            return listing.offering.slug
    return ""


def _lemon_total(payload: dict) -> tuple[Decimal, str]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    attributes = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    raw_total = attributes.get("total")
    currency = str(attributes.get("currency") or "USD").upper()
    try:
        minor = Decimal(str(raw_total))
    except Exception as exc:
        raise ChannelIngressError("LEMON_ORDER_TOTAL_INVALID") from exc
    amount = _money(minor / Decimal("100"))
    if amount <= 0:
        raise ChannelIngressError("LEMON_ORDER_TOTAL_INVALID")
    return amount, currency


@transaction.atomic
def receive_lemon_webhook(*, raw_body: bytes, signature: str) -> dict:
    if not verify_lemon_signature(raw_body, signature):
        raise ChannelIngressError("LEMON_WEBHOOK_SIGNATURE_INVALID", status=401)
    if not _truthy_env("LEMON_SQUEEZY_WEBHOOK_ENABLED"):
        raise ChannelIngressError("LEMON_SQUEEZY_WEBHOOK_DISABLED", status=503)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise ChannelIngressError("LEMON_WEBHOOK_JSON_INVALID") from exc
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    event_name = str(meta.get("event_name") or "").strip()
    if event_name != "order_created":
        AuditEvent.objects.create(
            event_type="channel.lemon_webhook_observed",
            actor="lemon-squeezy",
            metadata={"event_name": event_name or "UNKNOWN", "mutation_performed": False},
        )
        return {"event_name": event_name or "UNKNOWN", "handled": False, "mutation_performed": False}

    package_slug = _lemon_package_slug(payload)
    if not package_slug:
        raise ChannelIngressError("LEMON_PACKAGE_MAPPING_REQUIRED", status=409)
    listing = _listing(package_slug, market_slug=LEMON_MARKET)
    _require_public_runtime(listing, flag_name="LEMON_SQUEEZY_WEBHOOK_ENABLED", secret_name="LEMON_SQUEEZY_WEBHOOK_SECRET")
    price, currency = _lemon_total(payload)
    expected = _public_price(listing)
    if currency != listing.currency or price != expected:
        raise ChannelIngressError("LEMON_ORDER_PRICE_OR_CURRENCY_MISMATCH", status=409)

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    remote_order_id = str(data.get("id") or "").strip()
    if not remote_order_id:
        raise ChannelIngressError("LEMON_ORDER_ID_REQUIRED")
    custom = meta.get("custom_data") if isinstance(meta.get("custom_data"), dict) else {}
    requirements = custom.get("requirements") if isinstance(custom.get("requirements"), dict) else {}
    buyer_reference = str(custom.get("buyer_reference") or remote_order_id)[:255]
    fee = _platform_fee(listing, price)
    order, created = receive_inbound_order(
        marketplace=listing.marketplace,
        listing=listing,
        remote_order_id=f"lemon:{remote_order_id}",
        idempotency_key=f"lemon:{remote_order_id}",
        payload={
            "buyer_reference": buyer_reference,
            "requirements": requirements,
            "input_assets": [],
            "quoted_price": str(price),
            "platform_fee": str(fee),
            "currency": currency,
            "funding_state": "FUNDED",
            "remote_state": "RECEIVED",
            "acceptance_probability": "0.99",
        },
        authenticated_market_identity=True,
        authenticated_at=timezone.now(),
    )
    _enrich_job_inputs(order)
    missing = missing_buyer_inputs(order)
    if missing:
        order.status = InboundOrder.Status.PREFLIGHT_BLOCKED
        order.remote_state = "AWAITING_BUYER_INPUT"
        order.economic_preflight = {
            **(order.economic_preflight or {}),
            "eligible": False,
            "action_allowed": False,
            "reason_codes": list(dict.fromkeys([*(order.economic_preflight or {}).get("reason_codes", []), "BUYER_INPUT_REQUIRED"])),
            "missing_buyer_inputs": missing,
        }
        order.save(update_fields=["status", "remote_state", "economic_preflight", "updated_at"])
    return {"event_name": event_name, "handled": True, "order_id": str(order.id), "created": created, "missing_buyer_inputs": missing}


def _file_like_input(key: str) -> bool:
    return key in {"dataset", "source_data", "csv_input", "tabular_input", "image"}


def missing_buyer_inputs(order: InboundOrder) -> list[str]:
    if not order.listing_id:
        return ["LISTING_REQUIRED"]
    manifest = _package_manifest(order.listing)
    required = [str(item) for item in (manifest.get("buyer_inputs") or [])]
    requirements = order.requirements if isinstance(order.requirements, dict) else {}
    has_asset = bool(order.input_assets)
    missing = []
    for key in required:
        if _file_like_input(key):
            if not has_asset:
                missing.append(key)
        elif requirements.get(key) in (None, "", [], {}):
            missing.append(key)
    return missing


@transaction.atomic
def refresh_order_after_intake(order_id) -> InboundOrder:
    order = InboundOrder.objects.select_related("job", "listing__offering", "marketplace").select_for_update().get(pk=order_id)
    missing = missing_buyer_inputs(order)
    if missing:
        order.status = InboundOrder.Status.PREFLIGHT_BLOCKED
        order.remote_state = "AWAITING_BUYER_INPUT"
        order.save(update_fields=["status", "remote_state", "updated_at"])
        return order
    _enrich_job_inputs(order)
    run_inbound_economic_preflight(order)
    order.refresh_from_db()
    if order.status == InboundOrder.Status.READY:
        order.remote_state = "INPUT_COMPLETE"
        order.save(update_fields=["remote_state", "updated_at"])
    return order


def priority_channel_ingress_snapshot() -> dict:
    rapid_secret = bool(os.getenv("RAPIDAPI_PROXY_SECRET", "").strip())
    lemon_secret = bool(os.getenv("LEMON_SQUEEZY_WEBHOOK_SECRET", "").strip())
    rows = [
        {
            "market": "contra",
            "mode": "OWNER_MANUAL_IMPORT",
            "contract_implemented": True,
            "credential_configured": False,
            "receiver_enabled": True,
            "external_mutation_allowed": False,
            "truth": "Contra remains manual-first. Owner-imported real orders enter the canonical inbound lifecycle; no Contra account mutation is automated.",
        },
        {
            "market": "rapidapi",
            "mode": "PROXY_SECRET_ASYNC_API",
            "contract_implemented": True,
            "credential_configured": rapid_secret,
            "receiver_enabled": _truthy_env("RAPIDAPI_PUBLIC_INGRESS_ENABLED") and rapid_secret,
            "external_mutation_allowed": False,
            "truth": "Provider ingress requires the proxy secret, published listing proof, market/payout readiness, LOW_RISK/FULL mode, and inbound auto-accept before public traffic can execute.",
        },
        {
            "market": "apify-store",
            "mode": "APIFY_HOSTED_ACTOR",
            "contract_implemented": True,
            "credential_configured": False,
            "receiver_enabled": False,
            "external_mutation_allowed": False,
            "truth": "The Actor runtime is packaged in integrations/apify_actor and executes on Apify, not on Webdock.",
        },
        {
            "market": "lemon-squeezy",
            "mode": "SIGNED_WEBHOOK_PLUS_BUYER_INTAKE",
            "contract_implemented": True,
            "credential_configured": lemon_secret,
            "receiver_enabled": _truthy_env("LEMON_SQUEEZY_WEBHOOK_ENABLED") and lemon_secret,
            "external_mutation_allowed": False,
            "truth": "Signed order webhooks can create canonical inbound orders after publication/market readiness; missing bespoke inputs remain blocked until buyer intake completes.",
        },
    ]
    return {
        "section": "priority-channel-ingress",
        "rows": rows,
        "meta": {
            "contracts_implemented": sum(1 for row in rows if row["contract_implemented"]),
            "receivers_enabled": sum(1 for row in rows if row["receiver_enabled"]),
            "autonomy_mode": current_mode().value,
            "inbound_auto_accept_enabled": _truthy_env("INBOUND_SERVICE_AUTO_ACCEPT_ENABLED"),
            "banking_dependency_deferred": True,
            "external_mutation_allowed": False,
        },
    }
