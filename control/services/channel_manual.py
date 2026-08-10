from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, InboundOrder, MarketServiceListing
from control.services.channel_ingress import missing_buyer_inputs, refresh_order_after_intake
from control.services.seller_services import receive_inbound_order


CENT = Decimal("0.01")


class ManualChannelError(ValueError):
    pass


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _listing(package_slug: str) -> MarketServiceListing:
    try:
        return MarketServiceListing.objects.select_related("offering", "marketplace").get(
            offering__slug=package_slug,
            marketplace__slug="contra",
        )
    except MarketServiceListing.DoesNotExist as exc:
        raise ManualChannelError("CONTRA_PACKAGE_NOT_FOUND") from exc


def _conservative_fee(listing: MarketServiceListing, quoted_price: Decimal, explicit_fee: Any) -> Decimal:
    if explicit_fee not in (None, ""):
        fee = _money(_decimal(explicit_fee))
        if fee < 0 or fee > quoted_price:
            raise ManualChannelError("CONTRA_PLATFORM_FEE_INVALID")
        return fee
    metadata = listing.platform_metadata if isinstance(listing.platform_metadata, dict) else {}
    manifest = metadata.get("channel_package") if isinstance(metadata.get("channel_package"), dict) else {}
    assumptions = manifest.get("pricing_assumptions") if isinstance(manifest.get("pricing_assumptions"), dict) else {}
    reserve_rate = _decimal(assumptions.get("pricing_variable_rate_used"), "0.35")
    if reserve_rate < 0 or reserve_rate >= 1:
        reserve_rate = Decimal("0.35")
    return _money(quoted_price * reserve_rate)


@transaction.atomic
def receive_manual_contra_order(
    *,
    package_slug: str,
    remote_order_id: str,
    buyer_reference: str,
    quoted_price: Any,
    currency: str,
    requirements: dict | None,
    platform_fee: Any = None,
    funding_state: str = "UNVERIFIED",
    evidence_reference: str,
    actor: str = "owner",
) -> tuple[InboundOrder, bool]:
    listing = _listing(package_slug)
    reference = " ".join(str(evidence_reference or "").strip().split())[:700]
    if not remote_order_id.strip() or not reference:
        raise ManualChannelError("CONTRA_REMOTE_ORDER_AND_EVIDENCE_REQUIRED")
    price = _money(_decimal(quoted_price))
    floor = _money(_decimal(listing.offering.minimum_profitable_price))
    if price <= 0 or price < floor:
        raise ManualChannelError("CONTRA_QUOTE_BELOW_PROFITABLE_FLOOR")
    currency = str(currency or listing.currency).strip().upper()
    if currency != listing.currency:
        raise ManualChannelError("CONTRA_CURRENCY_MISMATCH")
    fee = _conservative_fee(listing, price, platform_fee)
    requirements = requirements if isinstance(requirements, dict) else {}
    idempotency = f"contra:{str(remote_order_id).strip()}"

    order, created = receive_inbound_order(
        marketplace=listing.marketplace,
        listing=listing,
        remote_order_id=idempotency,
        idempotency_key=idempotency,
        payload={
            "buyer_reference": str(buyer_reference or "")[:255],
            "requirements": requirements,
            "input_assets": [],
            "quoted_price": str(price),
            "platform_fee": str(fee),
            "currency": currency,
            "funding_state": str(funding_state or "UNVERIFIED")[:80],
            "remote_state": "MANUAL_ORDER_IMPORTED",
            "acceptance_probability": "0.95",
        },
        authenticated_market_identity=True,
        authenticated_at=timezone.now(),
    )
    if created:
        order.remote_state = "MANUAL_ORDER_IMPORTED"
        order.economic_preflight = {
            **(order.economic_preflight or {}),
            "manual_import_evidence_reference": reference,
            "platform_fee_semantics": "OWNER_SUPPLIED" if platform_fee not in (None, "") else "CONSERVATIVE_CATALOG_RESERVE",
        }
        order.save(update_fields=["remote_state", "economic_preflight", "updated_at"])
        order = refresh_order_after_intake(order.id)
        missing = missing_buyer_inputs(order)
        AuditEvent.objects.create(
            event_type="channel.contra_order_imported",
            actor=str(actor)[:120],
            metadata={
                "order_id": str(order.id),
                "package_slug": package_slug,
                "remote_order_id": str(remote_order_id)[:255],
                "evidence_reference": reference,
                "missing_buyer_inputs": missing,
                "external_mutation_performed": False,
            },
        )
    return order, created
