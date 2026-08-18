from __future__ import annotations

import hashlib
import secrets

from django.contrib.auth.hashers import make_password

from control.models import CommercialAPIKey, CommercialAPIProduct
from control.services.api_distribution import API_MARKET_BACKEND_LABEL_PREFIX
from control.services.commercial_api import (
    AuthenticatedAPIIdentity,
    CommercialAPIError,
    buyer_for_external_reference,
)


def contextualize_marketplace_identity(identity: AuthenticatedAPIIdentity, request, product: CommercialAPIProduct) -> AuthenticatedAPIIdentity:
    """Derive a stable buyer-scoped internal identity after gateway authentication.

    API.market documents a stable x-magicapi-user header. The private product
    backend key authenticates the marketplace itself; the forwarded user header
    is then used only for pseudonymous customer attribution, quota/idempotency
    isolation and economics. It is never accepted without the authenticated
    marketplace backend key.
    """
    if identity.source != "API_MARKET_PROXY":
        return identity

    buyer_ref = str(request.headers.get("X-Magicapi-User") or "").strip()
    if not buyer_ref:
        raise CommercialAPIError(
            "AUTHENTICATION_REQUIRED",
            status=401,
            detail="API.market requests require the authenticated forwarded buyer identity.",
        )

    buyer = buyer_for_external_reference(channel="api-market", external_reference=buyer_ref)
    digest = hashlib.sha256(f"{buyer.id}:{product.id}:{identity.key.plan_id}".encode()).hexdigest()[:14]
    key, _ = CommercialAPIKey.objects.get_or_create(
        prefix=f"apim-{digest}",
        defaults={
            "secret_hash": make_password(secrets.token_urlsafe(32)),
            "buyer": buyer,
            "plan": identity.key.plan,
            "label": f"{API_MARKET_BACKEND_LABEL_PREFIX} buyer {digest}",
        },
    )
    if key.buyer_id != buyer.id or key.plan_id != identity.key.plan_id:
        raise CommercialAPIError("AUTHENTICATION_REQUIRED", status=401)
    return AuthenticatedAPIIdentity(key=key, source="API_MARKET_PROXY")
