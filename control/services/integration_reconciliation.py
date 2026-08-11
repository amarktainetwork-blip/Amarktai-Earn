from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from control.models import AuditEvent, CommercePayment, MarketIntegrationProfile, WebhookEvent
from control.services.integration_accounts import BY_SLUG
from control.services.paystack_commerce import PaystackCommerceError, reconcile_payment, reconcile_paystack_settlements


def _paystack(limit: int) -> dict[str, Any]:
    rows = CommercePayment.objects.filter(provider="paystack", state__in=[CommercePayment.State.INITIALIZED, CommercePayment.State.UNKNOWN]).order_by("created_at")[:limit]
    reconciled = paid = failed = 0
    for payment in rows:
        try:
            result = reconcile_payment(payment.id)
            reconciled += 1
            paid += int(result.state == CommercePayment.State.PAID)
        except PaystackCommerceError as exc:
            if exc.code == "AUTHENTICATION":
                raise
            failed += 1
    settlements = reconcile_paystack_settlements(limit=limit)
    return {"checked": len(rows), "reconciled": reconciled, "paid": paid, "failed": failed, "settlements": settlements}


HANDLERS = {"paystack": _paystack}


def reconcile_integrations(*, limit_per_integration: int = 25, actor: str = "integration-watcher") -> dict[str, Any]:
    """One bounded, credential-aware reconciliation cycle; no blind external writes."""
    limit_per_integration = max(1, min(int(limit_per_integration), 100))
    profiles = list(
        MarketIntegrationProfile.objects.select_related("marketplace").filter(
            marketplace__slug__in=BY_SLUG,
            api_connection_state="VERIFIED",
        ).order_by("marketplace__slug")
    )
    results: dict[str, Any] = {}
    for profile in profiles:
        slug = profile.marketplace.slug
        if profile.last_error_category == "AUTHENTICATION" and profile.last_connection_test_at and profile.last_connection_test_at > timezone.now() - timedelta(hours=6):
            results[slug] = {"skipped": "AUTHENTICATION_BACKOFF"}
            continue
        handler = HANDLERS.get(slug)
        try:
            result = handler(limit_per_integration) if handler else {"checked": 0, "boundary": "WEBHOOK_OR_MANUAL_PROOF", "mutation_performed": False}
            profile.last_reconciled_at = timezone.now()
            profile.last_error_category = ""
            profile.last_safe_error = ""
            profile.save(update_fields=["last_reconciled_at", "last_error_category", "last_safe_error", "updated_at"])
            results[slug] = result
        except PaystackCommerceError as exc:
            profile.last_error_category = exc.code
            profile.last_safe_error = exc.safe_message[:300]
            if exc.code == "AUTHENTICATION":
                profile.api_connection_state = "UNVERIFIED"
                profile.live_proving_state = "BLOCKED"
                profile.autonomous_acquisition_enabled = False
                profile.marketplace.enabled = False
                profile.marketplace.payout_ready = False
                profile.marketplace.save(update_fields=["enabled", "payout_ready", "updated_at"])
            profile.save(update_fields=["last_error_category", "last_safe_error", "api_connection_state", "live_proving_state", "autonomous_acquisition_enabled", "updated_at"])
            results[slug] = {"error": exc.code, "safe_message": exc.safe_message}
    stale_before = timezone.now() - timedelta(hours=24)
    stale = list(
        MarketIntegrationProfile.objects.filter(api_connection_state="VERIFIED")
        .filter(last_reconciled_at__lt=stale_before)
        .values_list("marketplace__slug", flat=True)
    )
    failed_events = WebhookEvent.objects.filter(status__in=["FAILED", "UNKNOWN_EXTERNAL_STATE"]).count()
    AuditEvent.objects.create(event_type="integration.reconciliation_cycle", actor=actor[:120], metadata={"connected_integrations": len(profiles), "results": results, "stale_integrations": stale, "failed_external_events": failed_events, "bounded_limit": limit_per_integration})
    return {"connected_integrations": len(profiles), "results": results, "stale_integrations": stale, "failed_external_events": failed_events}
