from __future__ import annotations

import json

from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from control.models import AuditEvent
from control.services.auth_security import (
    Throttled,
    ensure_not_throttled,
    record_failure,
    reset,
    verify_reauthentication,
)
from control.services.payment_rails import payment_rail_snapshot, update_payment_rail_proof
from control.services.settlement_routes import settlement_routes_snapshot, update_market_settlement_route
from control.services.integration_accounts import integration_accounts_snapshot


def _json(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return {}


def _require_owner_reauthentication(request, *, event_prefix: str, subject_key: str):
    owner = getattr(request, "owner", None)
    if not owner:
        return None, None, JsonResponse({"error": "unauthorized"}, status=401)
    subject = str(owner.pk)
    try:
        ensure_not_throttled("reauth_user", subject)
    except Throttled:
        AuditEvent.objects.create(
            severity="WARN",
            event_type=f"{event_prefix}_throttled",
            actor=str(owner.pk),
            metadata={"subject": subject_key},
        )
        return owner, None, JsonResponse({"error": "reauthentication_failed"}, status=401)
    data = _json(request)
    password = str(data.pop("password", "") or "")
    code = str(data.pop("code", "") or "")
    if not verify_reauthentication(owner, password, code):
        record_failure("reauth_user", subject)
        AuditEvent.objects.create(
            severity="WARN",
            event_type=f"{event_prefix}_reauth_failed",
            actor=str(owner.pk),
            metadata={"subject": subject_key},
        )
        return owner, None, JsonResponse({"error": "reauthentication_failed"}, status=401)
    reset("reauth_user", subject)
    return owner, data, None


@require_GET
def banking_page(request):
    if not getattr(request, "owner", None):
        return redirect("login")
    return render(
        request,
        "control/banking.html",
        {
            "section": "banking",
            "page_title": "Treasury & Settlement",
            "page_description": "Automatic payout receipt, human withdrawals, crypto settlement, and account setup",
            "advanced_section": False,
        },
    )


@require_GET
def payment_rails_api(request):
    if not getattr(request, "owner", None):
        return JsonResponse({"error": "unauthorized"}, status=401)
    payload = payment_rail_snapshot()
    routes = settlement_routes_snapshot()
    payload["settlement_routes"] = routes["rows"]
    payload["settlement_route_meta"] = routes["meta"]
    payload["integration_accounts"] = integration_accounts_snapshot()
    return JsonResponse(payload)


@require_POST
def payment_rail_proof_api(request, slug):
    owner, data, error = _require_owner_reauthentication(
        request,
        event_prefix="treasury.payment_rail_update",
        subject_key=slug,
    )
    if error:
        return error
    try:
        row = update_payment_rail_proof(
            slug,
            status=data.get("status"),
            south_africa_verified=data.get("south_africa_verified", False),
            checkout_enabled=data.get("checkout_enabled", False),
            payout_receive_enabled=data.get("payout_receive_enabled", False),
            final_settlement_enabled=data.get("final_settlement_enabled", False),
            proof_reference=data.get("proof_reference", ""),
            owner_action=data.get("owner_action", ""),
            notes=data.get("notes", ""),
            actor=str(owner.pk),
        )
    except KeyError:
        return JsonResponse({"error": "unknown_payment_rail"}, status=404)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "rail": row})


@require_POST
def market_settlement_route_api(request, market_slug):
    owner, data, error = _require_owner_reauthentication(
        request,
        event_prefix="treasury.market_settlement_route_update",
        subject_key=market_slug,
    )
    if error:
        return error
    try:
        row = update_market_settlement_route(
            market_slug,
            status=data.get("status"),
            selected_rail=data.get("selected_rail", ""),
            proof_reference=data.get("proof_reference", ""),
            notes=data.get("notes", ""),
            actor=str(owner.pk),
        )
    except Exception as exc:
        if exc.__class__.__name__ == "DoesNotExist":
            return JsonResponse({"error": "unknown_market"}, status=404)
        if isinstance(exc, ValueError):
            return JsonResponse({"error": str(exc)}, status=400)
        raise
    return JsonResponse({"ok": True, "route": row})
