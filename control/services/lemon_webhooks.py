from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import json
import os
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, InboundOrder, MarketServiceListing, ServiceOffering
from control.services.channel_ingress import (
    ChannelIngressError,
    missing_buyer_inputs,
    refresh_order_after_intake,
    verify_lemon_signature,
)
from control.services.seller_services import receive_inbound_order, run_inbound_economic_preflight


CENT = Decimal("0.01")
LEMON_MARKET = "lemon-squeezy"


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _money(value: Decimal) -> Decimal:
    return max(Decimal("0"), value).quantize(CENT, rounding=ROUND_HALF_UP)


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def _data(payload: dict) -> tuple[str, dict]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return str(data.get("id") or "").strip(), data.get("attributes") if isinstance(data.get("attributes"), dict) else {}


def _custom(payload: dict) -> dict:
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    value = meta.get("custom_data")
    return value if isinstance(value, dict) else {}


def _event_name(payload: dict) -> str:
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    return str(meta.get("event_name") or "").strip()


def _listing_from_payload(payload: dict) -> MarketServiceListing:
    custom = _custom(payload)
    package_slug = str(custom.get("package_slug") or "").strip()
    if package_slug:
        listing = MarketServiceListing.objects.select_related("offering", "marketplace").filter(
            marketplace__slug=LEMON_MARKET,
            offering__slug=package_slug,
        ).first()
        if listing:
            return listing

    _, attributes = _data(payload)
    variant_id = ""
    first_item = attributes.get("first_order_item")
    if isinstance(first_item, dict):
        variant_id = str(first_item.get("variant_id") or "").strip()
    if not variant_id:
        variant_id = str(attributes.get("variant_id") or "").strip()
    if variant_id:
        listing = MarketServiceListing.objects.select_related("offering", "marketplace").filter(
            marketplace__slug=LEMON_MARKET,
            remote_listing_id=variant_id,
        ).first()
        if listing:
            return listing
    raise ChannelIngressError("LEMON_PACKAGE_MAPPING_REQUIRED", status=409)


def _require_published_mapping(listing: MarketServiceListing) -> None:
    if not (
        listing.status == MarketServiceListing.Status.PUBLISHED
        and listing.remote_listing_id
        and listing.remote_reference
        and listing.published_at
    ):
        raise ChannelIngressError("LEMON_PUBLICATION_EVIDENCE_REQUIRED", status=409)


def _commercial_revenue_basis(attributes: dict) -> tuple[Decimal, str]:
    currency = str(attributes.get("currency") or "USD").upper()
    total = _decimal(attributes.get("total"))
    tax = _decimal(attributes.get("tax"))
    amount = _money((total - tax) / Decimal("100"))
    if amount <= 0:
        raise ChannelIngressError("LEMON_REVENUE_BASIS_INVALID", status=409)
    return amount, currency


def _conservative_fee(listing: MarketServiceListing, price: Decimal) -> Decimal:
    metadata = listing.platform_metadata if isinstance(listing.platform_metadata, dict) else {}
    manifest = metadata.get("channel_package") if isinstance(metadata.get("channel_package"), dict) else {}
    assumptions = manifest.get("pricing_assumptions") if isinstance(manifest.get("pricing_assumptions"), dict) else {}
    rate = _decimal(assumptions.get("pricing_variable_rate_used"), str(listing.offering.platform_fee_rate))
    fixed = _decimal(assumptions.get("fixed_transaction_fee"), "0")
    if rate < 0 or rate >= 1 or fixed < 0:
        raise ChannelIngressError("LEMON_FEE_ASSUMPTIONS_INVALID", status=503)
    return min(price, _money((price * rate) + fixed))


def _order_requirements(payload: dict) -> dict:
    custom = _custom(payload)
    value = custom.get("requirements")
    return value if isinstance(value, dict) else {}


def _order_buyer_reference(payload: dict, fallback: str) -> str:
    custom = _custom(payload)
    return str(custom.get("buyer_reference") or fallback)[:255]


def _mark_missing_input(order: InboundOrder) -> list[str]:
    missing = missing_buyer_inputs(order)
    if not missing:
        return []
    preflight = dict(order.economic_preflight or {})
    preflight.update({
        "eligible": False,
        "action_allowed": False,
        "reason_codes": list(dict.fromkeys([*(preflight.get("reason_codes") or []), "BUYER_INPUT_REQUIRED"])),
        "missing_buyer_inputs": missing,
    })
    order.status = InboundOrder.Status.PREFLIGHT_BLOCKED
    order.remote_state = "AWAITING_BUYER_INPUT"
    order.economic_preflight = preflight
    order.save(update_fields=["status", "remote_state", "economic_preflight", "updated_at"])
    return missing


@transaction.atomic
def _handle_order_created(payload: dict) -> dict:
    remote_id, attributes = _data(payload)
    if not remote_id:
        raise ChannelIngressError("LEMON_ORDER_ID_REQUIRED")
    if str(attributes.get("status") or "").lower() not in {"paid", "pending"}:
        raise ChannelIngressError("LEMON_ORDER_NOT_PAID", status=409)
    listing = _listing_from_payload(payload)
    _require_published_mapping(listing)
    price, currency = _commercial_revenue_basis(attributes)
    if currency != listing.currency:
        raise ChannelIngressError("LEMON_ORDER_CURRENCY_MISMATCH", status=409)
    fee = _conservative_fee(listing, price)
    requirements = _order_requirements(payload)
    order, created = receive_inbound_order(
        marketplace=listing.marketplace,
        listing=listing,
        remote_order_id=f"lemon:{remote_id}",
        idempotency_key=f"lemon:{remote_id}",
        payload={
            "buyer_reference": _order_buyer_reference(payload, remote_id),
            "requirements": requirements,
            "input_assets": [],
            "quoted_price": str(price),
            "platform_fee": str(fee),
            "currency": currency,
            "funding_state": "FUNDED" if str(attributes.get("status") or "").lower() == "paid" else "AUTHORIZED",
            "remote_state": "LEMON_ORDER_RECEIVED",
            "acceptance_probability": "0.99",
        },
        authenticated_market_identity=True,
        authenticated_at=timezone.now(),
    )
    if created:
        preflight = dict(order.economic_preflight or {})
        preflight["lemon_order_id"] = remote_id
        preflight["lemon_test_mode"] = bool(attributes.get("test_mode"))
        order.economic_preflight = preflight
        order.save(update_fields=["economic_preflight", "updated_at"])
        order = refresh_order_after_intake(order.id)
    missing = _mark_missing_input(order)
    return {
        "event_name": "order_created",
        "handled": True,
        "order_id": str(order.id),
        "created": created,
        "missing_buyer_inputs": missing,
        "renewal": False,
    }


def _initial_order_by_lemon_order_id(order_id: str) -> InboundOrder | None:
    return InboundOrder.objects.select_related("listing__offering", "marketplace", "job").filter(
        marketplace__slug=LEMON_MARKET,
        remote_order_id=f"lemon:{order_id}",
    ).first()


def _subscription_order(subscription_id: str) -> InboundOrder | None:
    return InboundOrder.objects.select_related("listing__offering", "marketplace", "job").filter(
        marketplace__slug=LEMON_MARKET,
        economic_preflight__lemon_subscription_id=str(subscription_id),
    ).order_by("created_at").first()


@transaction.atomic
def _handle_subscription_state(payload: dict, event_name: str) -> dict:
    subscription_id, attributes = _data(payload)
    if not subscription_id:
        raise ChannelIngressError("LEMON_SUBSCRIPTION_ID_REQUIRED")
    order = _subscription_order(subscription_id)
    if order is None:
        order_id = str(attributes.get("order_id") or "").strip()
        order = _initial_order_by_lemon_order_id(order_id) if order_id else None
    if order is None:
        raise ChannelIngressError("LEMON_SUBSCRIPTION_ORDER_NOT_YET_RECORDED", status=409)
    if order.listing.offering.pricing_model != ServiceOffering.PricingModel.SUBSCRIPTION:
        raise ChannelIngressError("LEMON_SUBSCRIPTION_PACKAGE_MISMATCH", status=409)

    preflight = dict(order.economic_preflight or {})
    preflight.update({
        "lemon_subscription_id": subscription_id,
        "lemon_subscription_status": str(attributes.get("status") or ""),
        "lemon_subscription_variant_id": str(attributes.get("variant_id") or ""),
        "lemon_subscription_event": event_name,
        "lemon_subscription_updated_at": timezone.now().isoformat(),
    })
    order.economic_preflight = preflight
    order.save(update_fields=["economic_preflight", "updated_at"])
    AuditEvent.objects.create(
        event_type="channel.lemon_subscription_state",
        actor="lemon-squeezy",
        metadata={
            "event_name": event_name,
            "subscription_id": subscription_id,
            "order_id": str(order.id),
            "status": preflight["lemon_subscription_status"],
        },
    )
    return {"event_name": event_name, "handled": True, "order_id": str(order.id), "subscription_id": subscription_id, "mutation_performed": False}


@transaction.atomic
def _handle_subscription_payment_success(payload: dict) -> dict:
    invoice_id, attributes = _data(payload)
    if not invoice_id:
        raise ChannelIngressError("LEMON_SUBSCRIPTION_INVOICE_ID_REQUIRED")
    subscription_id = str(attributes.get("subscription_id") or "").strip()
    if not subscription_id:
        raise ChannelIngressError("LEMON_SUBSCRIPTION_ID_REQUIRED")
    if str(attributes.get("status") or "").lower() != "paid":
        raise ChannelIngressError("LEMON_SUBSCRIPTION_INVOICE_NOT_PAID", status=409)
    billing_reason = str(attributes.get("billing_reason") or "").lower()
    if billing_reason != "renewal":
        return {
            "event_name": "subscription_payment_success",
            "handled": True,
            "renewal": False,
            "subscription_id": subscription_id,
            "invoice_id": invoice_id,
            "mutation_performed": False,
        }

    original = _subscription_order(subscription_id)
    if original is None:
        raise ChannelIngressError("LEMON_SUBSCRIPTION_MAPPING_REQUIRED", status=409)
    listing = original.listing
    if listing is None or listing.offering.pricing_model != ServiceOffering.PricingModel.SUBSCRIPTION:
        raise ChannelIngressError("LEMON_SUBSCRIPTION_PACKAGE_MISMATCH", status=409)
    price, currency = _commercial_revenue_basis(attributes)
    if currency != original.currency:
        raise ChannelIngressError("LEMON_RENEWAL_CURRENCY_MISMATCH", status=409)
    fee = _conservative_fee(listing, price)
    order, created = receive_inbound_order(
        marketplace=listing.marketplace,
        listing=listing,
        remote_order_id=f"lemon-renewal:{invoice_id}",
        idempotency_key=f"lemon-renewal:{invoice_id}",
        payload={
            "buyer_reference": original.buyer_reference,
            "requirements": dict(original.requirements or {}),
            "input_assets": [],
            "quoted_price": str(price),
            "platform_fee": str(fee),
            "currency": currency,
            "funding_state": "FUNDED",
            "remote_state": "LEMON_SUBSCRIPTION_RENEWAL_RECEIVED",
            "acceptance_probability": "0.99",
        },
        authenticated_market_identity=True,
        authenticated_at=timezone.now(),
    )
    if created:
        preflight = dict(order.economic_preflight or {})
        preflight.update({
            "lemon_subscription_id": subscription_id,
            "lemon_subscription_invoice_id": invoice_id,
            "lemon_billing_reason": billing_reason,
            "lemon_parent_order_id": str(original.id),
            "lemon_test_mode": bool(attributes.get("test_mode")),
        })
        order.economic_preflight = preflight
        order.save(update_fields=["economic_preflight", "updated_at"])
        run_inbound_economic_preflight(order)
        order.refresh_from_db()
    missing = _mark_missing_input(order)
    return {
        "event_name": "subscription_payment_success",
        "handled": True,
        "renewal": True,
        "created": created,
        "order_id": str(order.id),
        "subscription_id": subscription_id,
        "invoice_id": invoice_id,
        "missing_buyer_inputs": missing,
    }


def dispatch_lemon_webhook(*, raw_body: bytes, signature: str) -> dict:
    if not verify_lemon_signature(raw_body, signature):
        raise ChannelIngressError("LEMON_WEBHOOK_SIGNATURE_INVALID", status=401)
    if not _truthy_env("LEMON_SQUEEZY_WEBHOOK_ENABLED"):
        raise ChannelIngressError("LEMON_SQUEEZY_WEBHOOK_DISABLED", status=503)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise ChannelIngressError("LEMON_WEBHOOK_JSON_INVALID") from exc
    event_name = _event_name(payload)
    if event_name == "order_created":
        return _handle_order_created(payload)
    if event_name in {
        "subscription_created",
        "subscription_updated",
        "subscription_cancelled",
        "subscription_resumed",
        "subscription_expired",
        "subscription_paused",
        "subscription_unpaused",
    }:
        return _handle_subscription_state(payload, event_name)
    if event_name == "subscription_payment_success":
        return _handle_subscription_payment_success(payload)

    AuditEvent.objects.create(
        event_type="channel.lemon_webhook_observed",
        actor="lemon-squeezy",
        metadata={"event_name": event_name or "UNKNOWN", "mutation_performed": False},
    )
    return {"event_name": event_name or "UNKNOWN", "handled": False, "mutation_performed": False}
