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
from control.services.market_control import (
    update_market_compliance_proof,
    update_market_operating_state,
)
from control.services.market_priority_dashboard import market_controls_snapshot


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
            actor=subject,
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
            actor=subject,
            metadata={"subject": subject_key},
        )
        return owner, None, JsonResponse({"error": "reauthentication_failed"}, status=401)
    reset("reauth_user", subject)
    return owner, data, None


@require_GET
def markets_page(request):
    if not getattr(request, "owner", None):
        return redirect("login")
    return render(
        request,
        "control/markets.html",
        {
            "section": "markets",
            "page_title": "Markets",
            "page_description": "Commercial priority, payout, settlement, and autonomy readiness",
            "advanced_section": False,
        },
    )


@require_GET
def market_controls_api(request):
    if not getattr(request, "owner", None):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(market_controls_snapshot())


@require_POST
def market_compliance_proof_api(request, market_slug):
    owner, data, error = _require_owner_reauthentication(
        request,
        event_prefix="market.compliance_proof_update",
        subject_key=market_slug,
    )
    if error:
        return error
    try:
        row = update_market_compliance_proof(
            market_slug,
            proof_type=data.get("proof_type", ""),
            verified=bool(data.get("verified", False)),
            proof_reference=data.get("proof_reference", ""),
            actor=str(owner.pk),
        )
    except Exception as exc:
        if exc.__class__.__name__ == "DoesNotExist":
            return JsonResponse({"error": "unknown_market"}, status=404)
        if isinstance(exc, ValueError):
            return JsonResponse({"error": str(exc)}, status=400)
        raise
    return JsonResponse({"ok": True, "market": row})


@require_POST
def market_operating_state_api(request, market_slug):
    owner, data, error = _require_owner_reauthentication(
        request,
        event_prefix="market.operating_state_update",
        subject_key=market_slug,
    )
    if error:
        return error
    try:
        row = update_market_operating_state(
            market_slug,
            enabled=bool(data.get("enabled", False)),
            autonomous_acquisition_enabled=bool(data.get("autonomous_acquisition_enabled", False)),
            actor=str(owner.pk),
        )
    except Exception as exc:
        if exc.__class__.__name__ == "DoesNotExist":
            return JsonResponse({"error": "unknown_market"}, status=404)
        if isinstance(exc, ValueError):
            return JsonResponse({"error": str(exc)}, status=400)
        raise
    return JsonResponse({"ok": True, "market": row})
