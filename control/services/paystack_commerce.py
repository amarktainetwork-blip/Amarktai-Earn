from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import requests
from django.db import transaction
from django.utils import timezone

from control.models import (
    AuditEvent,
    CommercePayment,
    FeePolicy,
    InboundOrder,
    MarketIntegrationProfile,
    Marketplace,
    MarketServiceListing,
    OwnerReceipt,
    ServiceOffering,
    WebhookEvent,
)
from control.services.integration_accounts import read_credentials
from control.services.seller_services import receive_inbound_order

CENT = Decimal("0.01")
PAYSTACK_BASE_URL = "https://api.paystack.co"
FEE_SOURCE = "https://paystack.com/za/pricing"


class PaystackCommerceError(RuntimeError):
    def __init__(self, code: str, safe_message: str, *, status: int = 400):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.status = status


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def ensure_paystack_fee_policy(marketplace: Marketplace) -> FeePolicy:
    policy, _ = FeePolicy.objects.get_or_create(
        marketplace=marketplace,
        policy_type="PAYMENT",
        currency="ZAR",
        source_version="ZA_2026-05-20_CONSERVATIVE_INTERNATIONAL",
        defaults={
            "percentage_rate": Decimal("0.031"),
            "fixed_fee": Decimal("1.00"),
            "tax_rate": Decimal("0.15"),
            "source_url": FEE_SOURCE,
            "effective_at": datetime(2026, 5, 20, tzinfo=timezone.get_current_timezone()),
            "verified": True,
            "active": True,
        },
    )
    return policy


def expected_fee(amount: Decimal, policy: FeePolicy) -> Decimal:
    base = (amount * policy.percentage_rate) + policy.fixed_fee
    return min(amount, _money(base + (base * policy.tax_rate)))


def _profile() -> MarketIntegrationProfile:
    profile = MarketIntegrationProfile.objects.select_related("marketplace").filter(marketplace__slug="paystack").first()
    if not profile or profile.api_connection_state != "VERIFIED":
        raise PaystackCommerceError("PAYSTACK_CONNECTION_REQUIRED", "Paystack must pass an authoritative connection test first.", status=409)
    return profile


def _request(session, method, url, **kwargs):
    try:
        response = session.request(method, url, timeout=15, **kwargs)
    except requests.Timeout as exc:
        raise PaystackCommerceError("UNKNOWN_EXTERNAL_MUTATION", "Paystack initialization timed out after the request; reconciliation is required.", status=502) from exc
    except requests.RequestException as exc:
        raise PaystackCommerceError("NETWORK", "Paystack could not be reached safely.", status=502) from exc
    if response.status_code in {401, 403}:
        raise PaystackCommerceError("AUTHENTICATION", "Paystack rejected the configured credentials.", status=409)
    if response.status_code == 429:
        raise PaystackCommerceError("RATE_LIMIT", "Paystack rate-limited the request; it was not replayed.", status=429)
    if not 200 <= response.status_code < 300:
        raise PaystackCommerceError("PROVIDER_REJECTION", f"Paystack rejected the request (HTTP {response.status_code}).", status=409)
    try:
        return response.json()
    except ValueError as exc:
        raise PaystackCommerceError("MALFORMED_RESPONSE", "Paystack returned a malformed response.", status=502) from exc


def initialize_checkout(*, offering_slug: str, customer_email: str, idempotency_key: str, proof_mode: bool = False, session=None) -> CommercePayment:
    profile = _profile()
    offering = ServiceOffering.objects.filter(slug=offering_slug).first()
    if not offering:
        raise PaystackCommerceError("OFFERING_NOT_FOUND", "The offering does not exist.", status=404)
    if not proof_mode and not (offering.enabled and offering.accepting_orders and profile.live_proving_state == "READY"):
        raise PaystackCommerceError("OFFERING_NOT_LIVE", "This offering is not enabled for live checkout.", status=409)
    email = str(customer_email or "").strip().casefold()
    if "@" not in email or len(email) > 320:
        raise PaystackCommerceError("CUSTOMER_EMAIL_INVALID", "A valid customer email is required.")
    idempotency_key = str(idempotency_key or "").strip()[:255]
    if not idempotency_key:
        raise PaystackCommerceError("IDEMPOTENCY_REQUIRED", "An idempotency key is required.")
    if offering.currency != "ZAR":
        raise PaystackCommerceError("PAYSTACK_ZAR_OFFERING_REQUIRED", "Paystack Direct currently requires a canonical ZAR price.", status=409)
    policy = ensure_paystack_fee_policy(profile.marketplace)
    amount = _money(offering.advertised_price)
    fee = expected_fee(amount, policy)
    total_cost = offering.expected_genx_cost + offering.expected_external_cost + offering.expected_operational_cost + fee
    if amount - total_cost <= 0 or amount < offering.minimum_profitable_price:
        raise PaystackCommerceError("NEGATIVE_MARGIN", "Profit Brain blocked checkout because the verified expected margin is not positive.", status=409)
    credentials = read_credentials("paystack")
    secret = credentials.get("secret_key", "")
    if not secret:
        raise PaystackCommerceError("PAYSTACK_CREDENTIALS_REQUIRED", "Paystack credentials are not configured.", status=409)
    if proof_mode and not secret.startswith("sk_test_"):
        raise PaystackCommerceError("PAYSTACK_TEST_KEY_REQUIRED", "Bounded checkout proof requires a Paystack test secret key.", status=409)
    reference = f"amarktai-{uuid.uuid4().hex}"
    with transaction.atomic():
        payment, created = CommercePayment.objects.get_or_create(
            provider="paystack",
            idempotency_key=idempotency_key,
            defaults={
                "marketplace": profile.marketplace,
                "offering": offering,
                "external_reference": reference,
                "amount": amount,
                "currency": offering.currency,
                "customer_reference_hash": hashlib.sha256(email.encode()).hexdigest(),
                "state": CommercePayment.State.CREATED,
                "provider_fee": fee,
                "evidence": {"fee_policy_id": policy.id, "fee_policy_version": policy.source_version, "proof_mode": proof_mode},
            },
        )
    if not created:
        expected_customer_hash = hashlib.sha256(email.encode()).hexdigest()
        if payment.offering_id != offering.id or payment.amount != amount or payment.currency != offering.currency or payment.customer_reference_hash != expected_customer_hash:
            raise PaystackCommerceError("IDEMPOTENCY_CONFLICT", "The idempotency key is already bound to a different checkout intent.", status=409)
        return payment
    body = {
        "email": email,
        "amount": str(int(amount * 100)),
        "currency": offering.currency,
        "reference": reference,
        "metadata": json.dumps({"commerce_payment_id": str(payment.id), "offering_slug": offering.slug}),
    }
    try:
        payload = _request(
            session or requests.Session(),
            "POST",
            f"{PAYSTACK_BASE_URL}/transaction/initialize",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json", "Accept": "application/json"},
            json=body,
        )
    except PaystackCommerceError as exc:
        if exc.code == "UNKNOWN_EXTERNAL_MUTATION":
            CommercePayment.objects.filter(pk=payment.pk, state=CommercePayment.State.CREATED).update(state=CommercePayment.State.UNKNOWN)
            payment.refresh_from_db()
        raise
    data = payload.get("data") if isinstance(payload, dict) else None
    if payload.get("status") is not True or not isinstance(data, dict) or data.get("reference") != reference or not data.get("authorization_url"):
        CommercePayment.objects.filter(pk=payment.pk, state=CommercePayment.State.CREATED).update(state=CommercePayment.State.UNKNOWN)
        raise PaystackCommerceError("MALFORMED_RESPONSE", "Paystack initialization returned an unexpected response; reconciliation is required.", status=502)
    with transaction.atomic():
        payment = CommercePayment.objects.select_for_update().get(pk=payment.pk)
        if payment.state == CommercePayment.State.CREATED:
            payment.state = CommercePayment.State.INITIALIZED
        payment.checkout_reference = str(data["authorization_url"])[:700]
        payment.evidence = {**payment.evidence, "access_code_present": bool(data.get("access_code")), "initialization_authoritative": False}
        payment.save(update_fields=["state", "checkout_reference", "evidence", "updated_at"])
    AuditEvent.objects.create(event_type="paystack.checkout_initialized", actor="paystack-commerce", metadata={"payment_id": str(payment.id), "reference": reference, "offering": offering.slug, "amount": str(amount), "currency": offering.currency, "revenue_recorded": False, "proof_mode": proof_mode})
    return payment


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    return bool(signature and hmac.compare_digest(expected, str(signature).strip()))


def _safe_event_payload(payload: dict) -> dict:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "event": str(payload.get("event") or "")[:120],
        "reference": str(data.get("reference") or data.get("transaction_reference") or "")[:255],
        "status": str(data.get("status") or "")[:80],
        "amount": str(data.get("amount") or "")[:40],
        "currency": str(data.get("currency") or "")[:12],
        "fees": str(data.get("fees") or "")[:40],
        "domain": str(data.get("domain") or "")[:20],
    }


@transaction.atomic
def dispatch_webhook(*, raw_body: bytes, signature: str) -> dict:
    market = Marketplace.objects.filter(slug="paystack").first()
    if not market:
        raise PaystackCommerceError("PAYSTACK_NOT_CONFIGURED", "Paystack is not configured.", status=503)
    raw_hash = hashlib.sha256(raw_body).hexdigest()
    event_key = hashlib.sha256(b"paystack:" + raw_body).hexdigest()
    existing = WebhookEvent.objects.filter(event_key=event_key).first()
    if existing:
        return {"duplicate": True, "event_id": existing.id, "status": existing.status}
    secret = read_credentials("paystack").get("secret_key", "")
    valid = bool(secret and verify_signature(raw_body, signature, secret))
    if not valid:
        event = WebhookEvent.objects.create(marketplace=market, event_key=event_key, event_type="UNAUTHENTICATED", signature_valid=False, raw_body_hash=raw_hash, status="REJECTED", error_code="INVALID_SIGNATURE", payload={})
        raise PaystackCommerceError("INVALID_SIGNATURE", "Paystack webhook signature is invalid.", status=401)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        WebhookEvent.objects.create(marketplace=market, event_key=event_key, event_type="MALFORMED", signature_valid=True, raw_body_hash=raw_hash, status="FAILED", error_code="INVALID_JSON", payload={})
        raise PaystackCommerceError("INVALID_JSON", "Paystack webhook body is invalid.") from exc
    safe = _safe_event_payload(payload)
    event_name = safe["event"]
    reference = safe["reference"]
    event = WebhookEvent.objects.create(
        marketplace=market,
        event_key=event_key,
        external_event_id=f"{event_name}:{reference}"[:255],
        event_type=event_name or "UNKNOWN",
        signature_valid=True,
        raw_body_hash=raw_hash,
        status="RECEIVED",
        payload=safe,
    )
    payment = CommercePayment.objects.select_for_update().filter(provider="paystack", external_reference=reference).first()
    if not payment:
        event.status = "UNKNOWN_EXTERNAL_STATE"
        event.unknown_external_state = True
        event.error_code = "PAYMENT_MAPPING_REQUIRED"
        event.save(update_fields=["status", "unknown_external_state", "error_code", "updated_at"])
        return {"duplicate": False, "handled": False, "status": event.status}
    event.commerce_payment = payment
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if event_name == "charge.success":
        amount = _money(Decimal(str(data.get("amount") or 0)) / Decimal("100"))
        currency = str(data.get("currency") or "").upper()
        if amount != payment.amount or currency != payment.currency or str(data.get("status") or "").casefold() != "success":
            event.status = "FAILED"
            event.error_code = "PAYMENT_EVIDENCE_MISMATCH"
            event.save(update_fields=["commerce_payment", "status", "error_code", "updated_at"])
            raise PaystackCommerceError("PAYMENT_EVIDENCE_MISMATCH", "Paystack paid evidence did not match the canonical payment intent.", status=409)
        fee = _money(Decimal(str(data.get("fees") or 0)) / Decimal("100"))
        listing, _ = MarketServiceListing.objects.get_or_create(
            offering=payment.offering,
            marketplace=market,
            defaults={"status": MarketServiceListing.Status.READY, "published_price": payment.amount, "currency": payment.currency, "pricing_model": payment.offering.pricing_model, "platform_metadata": {"channel": "PAYSTACK_DIRECT"}},
        )
        order, _created = receive_inbound_order(
            marketplace=market,
            listing=listing,
            remote_order_id=f"paystack:{reference}",
            idempotency_key=f"paystack:{reference}",
            payload={
                "buyer_reference": payment.customer_reference_hash,
                "requirements": {},
                "input_assets": [],
                "quoted_price": str(payment.amount),
                "platform_fee": str(fee),
                "currency": payment.currency,
                "funding_state": "FUNDED",
                "remote_state": "PAYSTACK_CHARGE_SUCCESS",
                "acceptance_probability": "0.99",
            },
            authenticated_market_identity=True,
            authenticated_at=timezone.now(),
        )
        payment.order = order
        payment.state = CommercePayment.State.PAID
        payment.authoritative = True
        payment.provider_fee = fee
        payment.paid_at = timezone.now()
        payment.evidence = {**payment.evidence, "charge_event_hash": raw_hash, "provider_transaction_id": str(data.get("id") or ""), "domain": str(data.get("domain") or "")}
        payment.save(update_fields=["order", "state", "authoritative", "provider_fee", "paid_at", "evidence", "updated_at"])
        from control.services.product_factory import record_owned_product_sale

        record_owned_product_sale(
            offering=payment.offering,
            channel="paystack",
            event_key=f"paystack:{reference}",
            gross=payment.amount,
        )
        OwnerReceipt.objects.get_or_create(
            marketplace=market,
            external_reference=reference,
            state=OwnerReceipt.State.PAYSTACK_BALANCE,
            defaults={"commerce_payment": payment, "rail": "paystack", "amount": payment.amount - fee, "currency": payment.currency, "authoritative": True, "human_withdrawal_required": False, "evidence": {"webhook_event_id": event.id, "raw_body_hash": raw_hash}},
        )
        event.inbound_order = order
        event.status = "PROCESSED"
        event.processed_at = timezone.now()
    elif event_name in {"refund.processed", "charge.dispute.create"}:
        if event_name == "refund.processed":
            refund_amount = _money(Decimal(str(data.get("amount") or 0)) / Decimal("100"))
            refund_currency = str(data.get("currency") or payment.currency).upper()
            if refund_amount != payment.amount or refund_currency != payment.currency:
                payment.state = CommercePayment.State.UNKNOWN
                payment.save(update_fields=["state", "updated_at"])
                event.status = "UNKNOWN_EXTERNAL_STATE"
                event.unknown_external_state = True
                event.error_code = "PARTIAL_REFUND_RECONCILIATION_REQUIRED"
                event.save(update_fields=["commerce_payment", "status", "unknown_external_state", "error_code", "updated_at"])
                return {"duplicate": False, "handled": False, "event": event_name, "payment_id": str(payment.id), "status": event.status}
        payment.state = CommercePayment.State.REVERSED if event_name == "refund.processed" else CommercePayment.State.UNKNOWN
        payment.reversed_at = timezone.now() if event_name == "refund.processed" else None
        payment.save(update_fields=["state", "reversed_at", "updated_at"])
        if payment.order_id and event_name == "refund.processed":
            InboundOrder.objects.filter(pk=payment.order_id).update(status=InboundOrder.Status.REVERSED, remote_state="PAYSTACK_REFUND_PROCESSED")
        if event_name == "refund.processed":
            from control.services.product_factory import record_owned_product_sale

            record_owned_product_sale(
                offering=payment.offering,
                channel="paystack",
                event_key=f"paystack:{reference}",
                gross=payment.amount,
                refunded=True,
            )
            OwnerReceipt.objects.get_or_create(marketplace=market, external_reference=reference, state=OwnerReceipt.State.REVERSED, defaults={"commerce_payment": payment, "rail": "paystack", "amount": payment.amount, "currency": payment.currency, "authoritative": True, "human_withdrawal_required": False, "evidence": {"webhook_event_id": event.id}})
        event.status = "PROCESSED"
        event.processed_at = timezone.now()
    else:
        event.status = "IGNORED"
        event.processed_at = timezone.now()
    event.save(update_fields=["commerce_payment", "inbound_order", "status", "processed_at", "updated_at"])
    return {"duplicate": False, "handled": event.status == "PROCESSED", "event": event_name, "payment_id": str(payment.id), "order_id": str(payment.order_id or ""), "status": event.status}


def reconcile_payment(payment_id, *, session=None) -> CommercePayment:
    payment = CommercePayment.objects.get(pk=payment_id, provider="paystack")
    secret = read_credentials("paystack").get("secret_key", "")
    payload = _request(
        session or requests.Session(),
        "GET",
        f"{PAYSTACK_BASE_URL}/transaction/verify/{payment.external_reference}",
        headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("reference") != payment.external_reference:
        raise PaystackCommerceError("MALFORMED_RESPONSE", "Paystack verification returned an unexpected response.", status=502)
    if str(data.get("status") or "").casefold() != "success":
        return payment
    synthetic = json.dumps({"event": "charge.success", "data": data}, sort_keys=True, separators=(",", ":")).encode()
    # Reconciliation is already authenticated by the secret-key verification call;
    # use the same canonical event processor with a locally generated valid signature.
    signature = hmac.new(secret.encode(), synthetic, hashlib.sha512).hexdigest()
    dispatch_webhook(raw_body=synthetic, signature=signature)
    payment.refresh_from_db()
    return payment
