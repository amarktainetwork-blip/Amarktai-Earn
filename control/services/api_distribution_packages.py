from __future__ import annotations

from django.db import transaction

from control.models import CommercialProductPackage
from control.services.api_distribution import API_MARKET_CHANNEL, POSTMAN_CHANNEL, ZYLA_CHANNEL


PACKAGE_CHANNELS = {
    "structured-data-cleanup": ["direct", "rapidapi", API_MARKET_CHANNEL, ZYLA_CHANNEL, POSTMAN_CHANNEL],
    "document-intelligence": ["direct", "rapidapi", API_MARKET_CHANNEL, POSTMAN_CHANNEL],
}


@transaction.atomic
def enrich_distribution_packages() -> dict[str, int]:
    updated = missing = 0
    for slug, channels in PACKAGE_CHANNELS.items():
        package = CommercialProductPackage.objects.filter(slug=slug).first()
        if package is None:
            missing += 1
            continue
        if list(package.channel_candidates or []) != channels:
            package.channel_candidates = channels
            package.save(update_fields=["channel_candidates", "updated_at"])
            updated += 1
    return {"updated": updated, "missing": missing, "total": len(PACKAGE_CHANNELS)}
