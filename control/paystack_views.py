from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from control.integration_views import _reauthenticated
from control.services.paystack_commerce import (
    PaystackCommerceError,
    dispatch_webhook,
    initialize_checkout,
)


def _json(request):
    try:
        value = json.loads(request.body or b"{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _error(exc):
    return JsonResponse({"error": exc.code, "message": exc.safe_message}, status=exc.status)


@require_POST
def paystack_checkout_api(request, offering_slug):
    data = _json(request)
    try:
        payment = initialize_checkout(offering_slug=offering_slug, customer_email=data.get("email", ""), idempotency_key=request.headers.get("Idempotency-Key", ""), proof_mode=False)
    except PaystackCommerceError as exc:
        return _error(exc)
    return JsonResponse({"payment_id": str(payment.id), "reference": payment.external_reference, "checkout_url": payment.checkout_reference, "state": payment.state})


@require_POST
def paystack_checkout_proof_api(request, offering_slug):
    _owner, data, error = _reauthenticated(request, action="paystack_checkout_proof", slug="paystack")
    if error:
        return error
    try:
        payment = initialize_checkout(offering_slug=offering_slug, customer_email=data.get("email", ""), idempotency_key=str(data.get("idempotency_key") or ""), proof_mode=True)
    except PaystackCommerceError as exc:
        return _error(exc)
    return JsonResponse({"payment_id": str(payment.id), "reference": payment.external_reference, "checkout_url": payment.checkout_reference, "state": payment.state, "revenue_recorded": False})


@csrf_exempt
@require_POST
def paystack_webhook_api(request):
    try:
        result = dispatch_webhook(raw_body=request.body, signature=request.headers.get("x-paystack-signature", ""))
    except PaystackCommerceError as exc:
        return _error(exc)
    return JsonResponse(result)
