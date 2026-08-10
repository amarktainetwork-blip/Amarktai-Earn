from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from control.services.channel_onboarding import (
    priority_channel_onboarding_snapshot,
    update_priority_channel_onboarding,
)
from control.services.channel_publication import (
    priority_publication_snapshot,
    record_priority_manual_publication,
)


def _json(request):
    try:
        return json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _owner(request):
    return getattr(request, "owner", None)


@require_GET
def priority_channel_onboarding_api(request):
    if not _owner(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(priority_channel_onboarding_snapshot())


@require_POST
def priority_channel_onboarding_update_api(request, market_slug):
    owner = _owner(request)
    if not owner:
        return JsonResponse({"error": "unauthorized"}, status=401)
    data = _json(request)
    try:
        row = update_priority_channel_onboarding(
            market_slug,
            checks=data.get("checks") if isinstance(data.get("checks"), dict) else {},
            proof_reference=data.get("proof_reference", ""),
            notes=data.get("notes", ""),
            actor=str(owner.pk),
        )
    except KeyError:
        return JsonResponse({"error": "unknown_priority_channel"}, status=404)
    except Exception as exc:
        if exc.__class__.__name__ == "DoesNotExist":
            return JsonResponse({"error": "unknown_priority_channel"}, status=404)
        if isinstance(exc, ValueError):
            return JsonResponse({"error": str(exc)}, status=400)
        raise
    return JsonResponse({"ok": True, "onboarding": row})


@require_GET
def priority_channel_publications_api(request):
    if not _owner(request):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(priority_publication_snapshot())


@require_POST
def priority_channel_publication_record_api(request, package_slug):
    owner = _owner(request)
    if not owner:
        return JsonResponse({"error": "unauthorized"}, status=401)
    data = _json(request)
    try:
        row = record_priority_manual_publication(
            package_slug,
            remote_listing_id=data.get("remote_listing_id", ""),
            remote_reference=data.get("remote_reference", ""),
            remote_version=data.get("remote_version", ""),
            evidence_reference=data.get("evidence_reference", ""),
            actor=str(owner.pk),
        )
    except KeyError:
        return JsonResponse({"error": "unknown_priority_package"}, status=404)
    except Exception as exc:
        if exc.__class__.__name__ == "DoesNotExist":
            return JsonResponse({"error": "unknown_priority_package"}, status=404)
        if isinstance(exc, ValueError):
            return JsonResponse({"error": str(exc)}, status=409)
        raise
    return JsonResponse({"ok": True, "publication": row})
