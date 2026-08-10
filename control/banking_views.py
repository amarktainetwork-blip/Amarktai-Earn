from __future__ import annotations

import json

from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from control.models import AuditEvent
from control.services.auth_security import (
    Throttled,
    client_ip,
    ensure_not_throttled,
    record_failure,
    reset,
    verify_reauthentication,
)
from control.services.payment_rails import payment_rail_snapshot, update_payment_rail_proof


def _json(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return {}


@require_GET
def banking_page(request):
    if not getattr(request, "owner", None):
        return redirect("login")
    return render(
        request,
        "control/banking.html",
        {
            "section": "banking",
            "page_title": "Banking",
            "page_description": "Owner payment rails, payout receipt, and final settlement proof",
            "advanced_section": False,
        },
    )


@require_GET
def payment_rails_api(request):
    if not getattr(request, "owner", None):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(payment_rail_snapshot())


@require_POST
def payment_rail_proof_api(request, slug):
    owner = getattr(request, "owner", None)
    if not owner:
        return JsonResponse({"error": "unauthorized"}, status=401)

    subject = f"{owner.pk}|{client_ip(request)}"
    try:
        ensure_not_throttled("treasury_reauth", subject)
    except Throttled:
        AuditEvent.objects.create(
            severity="WARN",
            event_type="treasury.payment_rail_update_throttled",
            actor=str(owner.pk),
            metadata={"rail": slug},
        )
        return JsonResponse({"error": "reauthentication_failed"}, status=401)

    data = _json(request)
    password = str(data.pop("password", "") or "")
    code = str(data.pop("code", "") or "")
    if not verify_reauthentication(owner, password, code):
        record_failure("treasury_reauth", subject)
        AuditEvent.objects.create(
            severity="WARN",
            event_type="treasury.payment_rail_update_reauth_failed",
            actor=str(owner.pk),
            metadata={"rail": slug},
        )
        return JsonResponse({"error": "reauthentication_failed"}, status=401)

    reset("treasury_reauth", subject)
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
