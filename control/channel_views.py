from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from control.services.channel_launch import priority_channel_launch_snapshot


@require_GET
def priority_channel_launch_api(request):
    if not getattr(request, "owner", None):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(priority_channel_launch_snapshot())
