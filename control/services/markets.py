from __future__ import annotations

import hashlib
import json
import os
from typing import Callable

from django.db import transaction
from django.utils import timezone

from control.models import (
    Job, MarketHealth, MarketIntegrationProfile, Marketplace, MarketPolicyVersion, PayoutAccount,
)
from control.services.jobs import ingest_opportunity
from markets.algora.client import AlgoraAdapter
from markets.callboard.client import CallboardAdapter
from markets.catalog import BY_SLUG, DEFINITIONS, MarketDefinition
from markets.dealwork.client import DealworkAdapter
from markets.opire.client import OpireAdapter
from markets.taskbounty.client import TaskBountyAdapter


def _policy_hash(definition: MarketDefinition) -> str:
    body = json.dumps(
        {"sources": definition.source_urls, "automation_allowed": definition.automation_allowed, "evidence": definition.evidence},
        sort_keys=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


@transaction.atomic
def bootstrap_market_integrations() -> dict[str, int]:
    created = updated = 0
    for definition in DEFINITIONS:
        market, was_created = Marketplace.objects.get_or_create(
            slug=definition.slug,
            defaults={
                "display_name": definition.display_name,
                "status": Marketplace.Status.PAYOUT_BLOCKED,
                "enabled": False,
                "payout_ready": False,
                "south_africa_verified": False,
                "payment_model": definition.payout_method,
            },
        )
        created += int(was_created)
        profile, profile_created = MarketIntegrationProfile.objects.update_or_create(
            marketplace=market,
            defaults={
                "adapter_name": definition.adapter_path,
                "adapter_version": "v1",
                "source_wired": True,
                "autonomous_acquisition_enabled": False,
                "policy_verified": bool(definition.capabilities.policy_verified),
                "docs_checked_at": timezone.now(),
                "auth_method": definition.auth_method,
                "rate_limit": definition.rate_limit,
                "payout_method": definition.payout_method,
                "capabilities": definition.capabilities.as_dict(),
                "source_urls": list(definition.source_urls),
                "blockers": list(definition.blockers),
                "evidence": definition.evidence,
            },
        )
        updated += int(not profile_created)
        policy_hash = _policy_hash(definition)
        MarketPolicyVersion.objects.update_or_create(
            marketplace=market,
            policy_hash=policy_hash,
            defaults={
                "source_url": definition.source_urls[0],
                "automation_allowed": definition.automation_allowed,
                "webdock_compatible": True,
                "checked_at": profile.docs_checked_at,
                "snapshot": {
                    "source_urls": list(definition.source_urls),
                    "capabilities": definition.capabilities.as_dict(),
                    "blockers": list(definition.blockers),
                    "evidence": definition.evidence,
                },
            },
        )
    return {"created": created, "updated": updated, "total": len(DEFINITIONS)}


def refresh_verified_payout_gate(market: Marketplace) -> bool:
    """Open payout fields only from a persisted, non-crypto, South-Africa-verified account."""
    account = PayoutAccount.objects.filter(
        marketplace=market,
        south_africa_verified=True,
        status__in=["ACTIVE", "VERIFIED", "READY"],
    ).exclude(rail__iregex=r"crypto|usdc|bitcoin|btc|ethereum|eth|solana").order_by("-verified_at", "-updated_at").first()
    if account is None:
        return False
    changed = []
    if not market.payout_ready:
        market.payout_ready = True
        changed.append("payout_ready")
    if not market.south_africa_verified:
        market.south_africa_verified = True
        changed.append("south_africa_verified")
    if changed:
        market.save(update_fields=[*changed, "updated_at"])
    return True


def configured_adapter(slug: str, *, source_readers: dict[str, Callable] | None = None):
    source_readers = source_readers or {}
    if slug == "agentgigs":
        from control.services.agentgigs import configured_adapter as configured_agentgigs
        return configured_agentgigs()
    if slug == "dealwork":
        return DealworkAdapter(os.getenv("DEALWORK_API_KEY", ""), base_url=os.getenv("DEALWORK_BASE_URL", "https://dealwork.ai"))
    if slug == "callboard":
        return CallboardAdapter(os.getenv("CALLBOARD_API_KEY", ""), base_url=os.getenv("CALLBOARD_BASE_URL", "https://getcallboard.com"))
    if slug == "taskbounty":
        return TaskBountyAdapter(os.getenv("TASKBOUNTY_API_KEY", ""), base_url=os.getenv("TASKBOUNTY_BASE_URL", "https://www.task-bounty.com/api/v1"))
    if slug == "opire":
        return OpireAdapter(source_readers.get(slug))
    if slug == "algora":
        return AlgoraAdapter(source_readers.get(slug))
    raise KeyError(f"unknown market adapter: {slug}")


def sync_market_discovery(slug: str, *, adapter=None, limit: int = 50) -> dict:
    definition = BY_SLUG[slug]
    market = Marketplace.objects.get(slug=slug)
    profile = market.integration_profile
    if not profile.source_wired or not profile.capabilities.get("discover"):
        return {"market": slug, "discovered": 0, "blocked": "DISCOVERY_NOT_SOURCE_WIRED"}
    try:
        adapter = adapter or configured_adapter(slug)
    except ValueError:
        return {"market": slug, "discovered": 0, "blocked": "MARKET_CREDENTIAL_NOT_CONFIGURED"}
    health = adapter.health()
    MarketHealth.objects.update_or_create(
        marketplace=market,
        defaults={
            "api_ok": bool(health.get("ok")),
            "auth_ok": bool(health.get("ok")),
            "payout_ok": bool(market.payout_ready and market.south_africa_verified),
            "supply_ok": False,
            "last_error_code": "" if health.get("ok") else str(health.get("error") or "SOURCE_NOT_CONFIGURED")[:120],
            "checked_at": timezone.now(),
            "details": {"health": health, "live_external_proof": False},
        },
    )
    if not health.get("ok"):
        return {"market": slug, "discovered": 0, "blocked": "MARKET_HEALTH_NOT_OK"}
    discovered = 0
    for raw in adapter.discover_jobs(limit=max(1, min(int(limit), 100))):
        opportunity = adapter.normalize_job(raw)
        if not opportunity.external_id or opportunity.reward < 0:
            continue
        ingest_opportunity(market, opportunity)
        discovered += 1
    MarketHealth.objects.filter(marketplace=market).update(supply_ok=discovered > 0)
    return {"market": slug, "discovered": discovered, "jobs_total": Job.objects.filter(marketplace=market).count()}
