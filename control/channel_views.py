from __future__ import annotations

import json
import os
from pathlib import Path

from django.http import FileResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from control.models import Artifact, InboundOrder
from control.services.channel_commercial import (
    priority_channel_commercial_pricing_snapshot,
    set_owner_commercial_price,
)
from control.services.channel_exports import priority_channel_export_snapshot
from control.services.channel_ingress import (
    ChannelIngressError,
    priority_channel_ingress_snapshot,
    rapidapi_order_artifact,
    rapidapi_order_snapshot,
    receive_lemon_webhook,
    receive_rapidapi_call,
)
from control.services.channel_launch import priority_channel_launch_snapshot
from control.services.channel_manual import ManualChannelError, receive_manual_contra_order
from control.services.inbound_controller import (
    InboundControllerError,
    accept_inbound_order,
    dispatch_accepted_inbound_orders,
    record_manual_inbound_delivery,
)
from control.services.inbound_portal import (
    IntakePortalError,
    intake_snapshot,
    issue_intake_token,
    resolve_intake_token,
    submit_buyer_intake,
)
from planning.models import WorkPlan


def _json(request):
    try:
        return json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _require_owner(request):
    return getattr(request, "owner", None)


def _owner_order_snapshot(order: InboundOrder) -> dict:
    order = InboundOrder.objects.select_related("job", "marketplace", "listing__offering").get(pk=order.pk)
    plan = WorkPlan.objects.filter(job_id=order.job_id).order_by("-updated_at").first()
    artifacts = Artifact.objects.filter(job_id=order.job_id).order_by("id")
    return {
        "order_id": str(order.id),
        "market": order.marketplace.slug,
        "package_slug": order.listing.offering.slug if order.listing_id else None,
        "package_name": order.listing.offering.display_name if order.listing_id else None,
        "buyer_reference": order.buyer_reference,
        "order_status": order.status,
        "remote_state": order.remote_state,
        "job_id": str(order.job_id) if order.job_id else None,
        "job_state": order.job.state if order.job_id else None,
        "quoted_price": str(order.quoted_price),
        "platform_fee": str(order.platform_fee),
        "currency": order.currency,
        "funding_state": order.funding_state,
        "workplan_state": plan.status if plan else None,
        "preflight": order.economic_preflight,
        "artifacts": [
            {"id": row.id, "mime_type": row.mime_type, "size_bytes": row.size_bytes}
            for row in artifacts
        ],
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
    }


@require_GET
def priority_channel_launch_api(request):
    if not _require_owner(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(priority_channel_launch_snapshot())


@require_GET
def priority_channel_publication_exports_api(request):
    if not _require_owner(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(priority_channel_export_snapshot())


@require_GET
def priority_channel_commercial_pricing_api(request):
    if not _require_owner(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(priority_channel_commercial_pricing_snapshot())


@require_POST
def priority_channel_commercial_price_api(request, package_slug):
    owner = _require_owner(request)
    if not owner:
        return JsonResponse({"error": "unauthorized"}, status=401)
    data = _json(request)
    try:
        row = set_owner_commercial_price(package_slug, price=data.get("price"), actor=str(owner.pk))
    except InboundOrder.DoesNotExist:
        return JsonResponse({"error": "unknown_package"}, status=404)
    except Exception as exc:
        if exc.__class__.__name__ == "DoesNotExist":
            return JsonResponse({"error": "unknown_package"}, status=404)
        if isinstance(exc, ValueError):
            return JsonResponse({"error": str(exc)}, status=400)
        raise
    return JsonResponse({"ok": True, "pricing": row})


@require_GET
def priority_channel_ingress_api(request):
    if not _require_owner(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(priority_channel_ingress_snapshot())


@require_GET
def priority_inbound_orders_api(request):
    if not _require_owner(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    rows = [
        _owner_order_snapshot(order)
        for order in InboundOrder.objects.select_related("job", "marketplace", "listing__offering")
        .filter(marketplace__slug__in=["contra", "rapidapi", "apify-store", "lemon-squeezy"])
        .order_by("-created_at")[:100]
    ]
    return JsonResponse({
        "section": "priority-inbound-orders",
        "rows": rows,
        "meta": {
            "total_returned": len(rows),
            "banking_dependency_deferred": True,
            "truth": "Inbound orders are operating obligations, not received cash. Banking and settlement activation remain a separate final phase.",
        },
    })


@require_GET
def priority_inbound_order_api(request, order_id):
    if not _require_owner(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    try:
        order = InboundOrder.objects.get(pk=order_id)
    except InboundOrder.DoesNotExist:
        return JsonResponse({"error": "unknown_order"}, status=404)
    return JsonResponse(_owner_order_snapshot(order))


@require_POST
def contra_manual_order_api(request):
    owner = _require_owner(request)
    if not owner:
        return JsonResponse({"error": "unauthorized"}, status=401)
    data = _json(request)
    try:
        order, created = receive_manual_contra_order(
            package_slug=str(data.get("package_slug") or ""),
            remote_order_id=str(data.get("remote_order_id") or ""),
            buyer_reference=str(data.get("buyer_reference") or ""),
            quoted_price=data.get("quoted_price"),
            currency=str(data.get("currency") or "USD"),
            requirements=data.get("requirements") if isinstance(data.get("requirements"), dict) else {},
            platform_fee=data.get("platform_fee"),
            funding_state=str(data.get("funding_state") or "UNVERIFIED"),
            evidence_reference=str(data.get("evidence_reference") or ""),
            actor=str(owner.pk),
        )
    except ManualChannelError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"created": created, "order": _owner_order_snapshot(order)}, status=201 if created else 200)


@require_POST
def inbound_order_accept_api(request, order_id):
    owner = _require_owner(request)
    if not owner:
        return JsonResponse({"error": "unauthorized"}, status=401)
    try:
        order = accept_inbound_order(order_id, actor=str(owner.pk), manual=True)
        dispatch = dispatch_accepted_inbound_orders(limit=20)
    except InboundOrder.DoesNotExist:
        return JsonResponse({"error": "unknown_order"}, status=404)
    except (InboundControllerError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({"ok": True, "order": _owner_order_snapshot(order), "dispatch": dispatch})


@require_POST
def inbound_order_delivery_api(request, order_id):
    owner = _require_owner(request)
    if not owner:
        return JsonResponse({"error": "unauthorized"}, status=401)
    data = _json(request)
    try:
        order = record_manual_inbound_delivery(
            order_id,
            remote_reference=str(data.get("remote_reference") or ""),
            actor=str(owner.pk),
        )
    except InboundOrder.DoesNotExist:
        return JsonResponse({"error": "unknown_order"}, status=404)
    except (InboundControllerError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({"ok": True, "order": _owner_order_snapshot(order)})


@require_POST
def inbound_order_intake_link_api(request, order_id):
    owner = _require_owner(request)
    if not owner:
        return JsonResponse({"error": "unauthorized"}, status=401)
    try:
        order = InboundOrder.objects.get(pk=order_id)
        token = issue_intake_token(order, actor=str(owner.pk))
    except InboundOrder.DoesNotExist:
        return JsonResponse({"error": "unknown_order"}, status=404)
    except IntakePortalError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    path = f"/intake/{token}/"
    return JsonResponse({"ok": True, "order_id": str(order.id), "intake_path": path, "intake_url": request.build_absolute_uri(path)})


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
def inbound_intake_page(request, token):
    try:
        order = resolve_intake_token(token)
    except IntakePortalError as exc:
        return render(request, "control/intake.html", {"error": str(exc)}, status=410 if "EXPIRED" in str(exc) else 404)
    submitted = False
    if request.method == "POST":
        fields = {key: value for key, value in request.POST.items() if key != "csrfmiddlewaretoken"}
        uploads = list(request.FILES.values())
        try:
            order = submit_buyer_intake(order, fields=fields, uploads=uploads)
            submitted = True
        except (IntakePortalError, ValueError) as exc:
            return render(request, "control/intake.html", {"error": str(exc), "snapshot": intake_snapshot(order)}, status=400)
    return render(request, "control/intake.html", {"snapshot": intake_snapshot(order), "submitted": submitted})


@csrf_exempt
@require_POST
def rapidapi_package_api(request, package_slug):
    secret = str(request.headers.get("X-RapidAPI-Proxy-Secret") or "")
    rapid_user = str(request.headers.get("X-RapidAPI-User") or "")
    try:
        body = json.loads(request.body or b"{}")
        order, created = receive_rapidapi_call(
            package_slug=package_slug,
            body=body,
            proxy_secret=secret,
            rapid_user=rapid_user,
        )
        if order.status == InboundOrder.Status.READY:
            order = accept_inbound_order(order.id, actor="rapidapi-provider", manual=False)
        dispatch = dispatch_accepted_inbound_orders(limit=20)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "RAPIDAPI_BODY_INVALID"}, status=400)
    except ChannelIngressError as exc:
        return JsonResponse({"error": exc.code}, status=exc.status)
    except (InboundControllerError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({
        "accepted": True,
        "created": created,
        "order_id": str(order.id),
        "status_path": f"/api/channels/rapidapi/orders/{order.id}",
        "order_status": order.status,
        "job_state": order.job.state,
        "dispatch": dispatch,
    }, status=202)


@require_GET
def rapidapi_order_api(request, order_id):
    try:
        payload = rapidapi_order_snapshot(
            order_id,
            proxy_secret=str(request.headers.get("X-RapidAPI-Proxy-Secret") or ""),
            rapid_user=str(request.headers.get("X-RapidAPI-User") or ""),
        )
    except ChannelIngressError as exc:
        return JsonResponse({"error": exc.code}, status=exc.status)
    return JsonResponse(payload)


@require_GET
def rapidapi_order_artifact_api(request, order_id, artifact_id):
    try:
        artifact = rapidapi_order_artifact(
            order_id,
            artifact_id,
            proxy_secret=str(request.headers.get("X-RapidAPI-Proxy-Secret") or ""),
            rapid_user=str(request.headers.get("X-RapidAPI-User") or ""),
        )
    except ChannelIngressError as exc:
        return JsonResponse({"error": exc.code}, status=exc.status)
    path = Path(artifact.path).resolve()
    job_root = Path(os.getenv("AMARKTAI_JOB_ROOT", "/var/lib/amarktai-earn/jobs")).resolve()
    if not path.is_file() or (path != job_root and job_root not in path.parents):
        return JsonResponse({"error": "RAPIDAPI_ARTIFACT_STORAGE_INVALID"}, status=500)
    return FileResponse(path.open("rb"), content_type=artifact.mime_type or "application/octet-stream", filename=path.name)


@csrf_exempt
@require_POST
def lemon_squeezy_webhook_api(request):
    try:
        result = receive_lemon_webhook(
            raw_body=request.body,
            signature=str(request.headers.get("X-Signature") or ""),
        )
        order_id = result.get("order_id")
        dispatch = None
        if result.get("handled") and order_id and not result.get("missing_buyer_inputs"):
            order = InboundOrder.objects.get(pk=order_id)
            if order.status == InboundOrder.Status.READY:
                accept_inbound_order(order.id, actor="lemon-squeezy", manual=False)
            dispatch = dispatch_accepted_inbound_orders(limit=20)
    except ChannelIngressError as exc:
        return JsonResponse({"error": exc.code}, status=exc.status)
    except (InboundControllerError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({**result, "dispatch": dispatch})
