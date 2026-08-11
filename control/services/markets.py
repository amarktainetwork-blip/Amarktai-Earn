from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
import json
import os
from typing import Callable

from django.db import transaction
from django.utils import timezone

from control.models import (
    Job, MarketHealth, MarketIntegrationProfile, Marketplace, MarketPolicyVersion, PayoutAccount,
)
from control.services.demand_pipeline import qualify_and_shadow_score
from control.services.jobs import ingest_opportunity
from control.services.market_control import DEALWORK_KYA_BLOCKER, overlay_operator_profile_truth
from markets.algora.client import AlgoraAdapter
from markets.callboard.client import CallboardAdapter
from markets.catalog import BY_SLUG, DEFINITIONS, MarketDefinition
from markets.dealwork.client import DealworkAdapter
from markets.opire.client import OpireAdapter
from markets.taskbounty.client import TaskBountyAdapter


_BOUNTY_DISCOVERY_MARKETS = frozenset({"taskbounty", "opire", "algora"})


def _policy_hash(definition: MarketDefinition) -> str:
    body = json.dumps(
        {"sources": definition.source_urls, "automation_allowed": definition.automation_allowed, "evidence": definition.evidence},
        sort_keys=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def _trusted_discovery_payload(definition: MarketDefinition, payload: dict | None) -> dict:
    """Attach controller-owned source provenance before demand qualification.

    Upstream job/bounty APIs do not all expose a common `type`/requirements shape.
    The controller does know which canonical adapter/source produced the row, so it
    records that fact in reserved fields without overwriting the upstream payload.
    Explicit upstream seller/test evidence is still evaluated first by the
    qualifier and therefore remains fail-closed.
    """
    enriched = dict(payload) if isinstance(payload, dict) else {}
    enriched["_amarktai_source_type"] = (
        "bounty" if definition.slug in _BOUNTY_DISCOVERY_MARKETS else "posted_job"
    )
    enriched["_amarktai_source_market"] = definition.slug
    enriched["_amarktai_source_adapter"] = definition.adapter_path
    return enriched


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

        existing_profile = MarketIntegrationProfile.objects.filter(marketplace=market).first()
        evidence, blockers = overlay_operator_profile_truth(
            definition.slug,
            base_evidence=definition.evidence,
            base_blockers=definition.blockers,
            existing_evidence=existing_profile.evidence if existing_profile else None,
        )
        preserved_acquisition = bool(
            existing_profile
            and existing_profile.autonomous_acquisition_enabled
            and definition.capabilities.policy_verified
            and market.enabled
            and market.status == Marketplace.Status.LIVE
            and not (definition.slug == "dealwork" and DEALWORK_KYA_BLOCKER in blockers)
        )

        profile, profile_created = MarketIntegrationProfile.objects.update_or_create(
            marketplace=market,
            defaults={
                "adapter_name": definition.adapter_path,
                "adapter_version": "v1",
                "source_wired": True,
                "autonomous_acquisition_enabled": preserved_acquisition,
                "policy_verified": bool(definition.capabilities.policy_verified),
                "docs_checked_at": timezone.now(),
                "auth_method": definition.auth_method,
                "rate_limit": definition.rate_limit,
                "payout_method": definition.payout_method,
                "capabilities": definition.capabilities.as_dict(),
                "source_urls": list(definition.source_urls),
                "blockers": blockers,
                "evidence": evidence,
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
                "webdock_compatible": definition.webdock_compatible,
                "checked_at": profile.docs_checked_at,
                "snapshot": {
                    "source_urls": list(definition.source_urls),
                    "capabilities": definition.capabilities.as_dict(),
                    "blockers": list(definition.blockers),
                    "evidence": definition.evidence,
                    "webdock_compatible": definition.webdock_compatible,
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

    discovered = scored = buyer_demand = 0
    classifications: Counter[str] = Counter()
    for raw in adapter.discover_jobs(limit=max(1, min(int(limit), 100))):
        opportunity = adapter.normalize_job(raw)
        if not opportunity.external_id or opportunity.reward < 0:
            continue
        opportunity = replace(
            opportunity,
            raw=_trusted_discovery_payload(definition, opportunity.raw),
        )
        job, _ = ingest_opportunity(market, opportunity)
        result = qualify_and_shadow_score(job)
        classification = str(result.get("classification") or "UNKNOWN")
        classifications[classification] += 1
        buyer_demand += int(classification == "BUYER_DEMAND")
        scored += int(bool(result.get("scored")))
        discovered += 1

    MarketHealth.objects.filter(marketplace=market).update(supply_ok=discovered > 0)
    return {
        "market": slug,
        "discovered": discovered,
        "buyer_demand": buyer_demand,
        "scored": scored,
        "qualification_counts": dict(sorted(classifications.items())),
        "jobs_total": Job.objects.filter(marketplace=market).count(),
    }
