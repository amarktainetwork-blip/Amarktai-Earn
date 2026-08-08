from __future__ import annotations

import json
import os

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from control.models import MarketplaceCredential, WebhookEvent
from control.queueing import enqueue_agentgigs_webhook
from control.secrets import decrypt_secret
from control.services.agentgigs import ensure_marketplace
from markets.agentgigs.webhooks import AgentGigsWebhookError, event_key, parse_webhook, verify_signature


MAX_WEBHOOK_BYTES = 1024 * 1024


def _agentgigs_webhook_secret() -> str:
    env_secret = os.getenv("AGENTGIGS_WEBHOOK_SECRET", "")
    if env_secret:
        return env_secret
    market = ensure_marketplace()
    credential = MarketplaceCredential.objects.filter(
        marketplace=market,
        credential_type="webhook_secret",
        active=True,
    ).order_by("-updated_at").first()
    return decrypt_secret(credential.encrypted_value) if credential else ""


@csrf_exempt
@require_POST
def agentgigs_webhook(request):
    raw = request.body
    if len(raw) > MAX_WEBHOOK_BYTES:
        return JsonResponse({"error": "payload_too_large"}, status=413)
    secret = _agentgigs_webhook_secret()
    if not secret:
        return JsonResponse({"error": "webhook_not_configured"}, status=503)
    signature = request.headers.get("X-AgentGigs-Signature", "")
    if not verify_signature(raw, signature, secret):
        return JsonResponse({"error": "invalid_signature"}, status=401)
    try:
        parsed = parse_webhook(raw)
    except AgentGigsWebhookError:
        return JsonResponse({"error": "invalid_payload"}, status=400)

    market = ensure_marketplace()
    payload = {"event": parsed.event, "timestamp": parsed.timestamp, "data": parsed.data}
    row, created = WebhookEvent.objects.get_or_create(
        event_key=event_key(raw),
        defaults={
            "marketplace": market,
            "event_type": parsed.event,
            "external_job_id": parsed.external_job_id,
            "occurred_at_remote": parsed.timestamp,
            "payload": payload,
        },
    )
    queued = False
    if created or row.status in {"RECEIVED", "FAILED"}:
        try:
            enqueue_agentgigs_webhook(row.id, row.event_type)
            queued = True
        except Exception:
            # The event is durable in PostgreSQL. A periodic reconciliation task can
            # process it later even if Redis is temporarily unavailable.
            queued = False
    return JsonResponse({"accepted": True, "duplicate": not created, "queued": queued}, status=202)
