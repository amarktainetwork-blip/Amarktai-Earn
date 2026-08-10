from __future__ import annotations

import os
from datetime import timedelta

from django.utils import timezone

from control.models import Marketplace
from control.services.autonomy import current_mode


# Explicitly reviewed proving policy. These markets can hold approved earnings
# inside the marketplace while final withdrawal remains separately unverified.
# This is never equivalent to cash settlement readiness.
PLATFORM_WALLET_PROVING_MARKETS = frozenset({"dealwork"})

# Marketplace profile blocker codes that are genuinely required before taking
# posted work. Banking/withdrawal and seller-listing blockers are deliberately
# not copied here; their truth remains visible in their own readiness domains.
WORK_BLOCKERS_BY_MARKET = {
    "agentgigs": frozenset(),
    "dealwork": frozenset({"DEALWORK_KYA_NOT_VERIFIED"}),
    "callboard": frozenset({"AGENT_OWNER_CLAIM_NOT_VERIFIED"}),
    "taskbounty": frozenset(),
    "opire": frozenset({"NO_DOCUMENTED_PUBLIC_SOLVER_API", "REWARD_CREATOR_PAYMENT_NOT_ESCROWED"}),
    "algora": frozenset({"SOLVER_MUTATION_AUTH_NOT_VERIFIED"}),
}


def _auto_switch_name(slug: str) -> str:
    if slug == "agentgigs":
        return "AGENTGIGS_AUTO_APPLY_ENABLED"
    return f"{slug.upper().replace('-', '_')}_AUTO_ACQUIRE_ENABLED"


def _policy_current(policy) -> bool:
    if policy is None:
        return False
    max_age = max(1, int(os.getenv("MARKET_POLICY_MAX_AGE_DAYS", "30")))
    return bool(policy.checked_at >= timezone.now() - timedelta(days=max_age))


def acquisition_cash_gate_required(market: Marketplace) -> bool:
    """Whether final cash-route proof is required before acquiring posted work.

    Dealwork is the only reviewed proving exception. Earnings there may remain in
    the marketplace wallet while withdrawal is unresolved. This function must not
    be reused to report SETTLED cash or to activate seller storefronts.
    """
    return market.slug not in PLATFORM_WALLET_PROVING_MARKETS


def market_readiness(market: Marketplace) -> dict:
    try:
        profile = market.integration_profile
    except Exception:
        profile = None
    try:
        health = market.health_snapshot
    except Exception:
        health = None
    policy = market.policy_versions.order_by("-checked_at", "-created_at").first()

    connected = bool(health and health.auth_ok)
    source_wired = bool(profile and profile.source_wired and profile.capabilities.get("discover"))
    policy_current = _policy_current(policy)
    policy_verified = bool(policy and policy.automation_allowed and profile and profile.policy_verified)

    work_blockers: list[str] = []
    if not connected:
        work_blockers.append("MARKET_NOT_CONNECTED")
    if not source_wired:
        work_blockers.append("DISCOVERY_NOT_SOURCE_WIRED")
    if not policy_verified:
        work_blockers.append("AUTOMATION_POLICY_NOT_APPROVED")
    if not policy_current:
        work_blockers.append("MARKET_AUTOMATION_POLICY_STALE")
    if policy and not policy.webdock_compatible:
        work_blockers.append("MARKET_RUNTIME_NOT_COMPATIBLE")
    if profile:
        relevant = WORK_BLOCKERS_BY_MARKET.get(market.slug, frozenset())
        work_blockers.extend(code for code in (profile.blockers or []) if code in relevant)
    else:
        work_blockers.append("MARKET_INTEGRATION_PROFILE_MISSING")

    work_blockers = list(dict.fromkeys(work_blockers))
    work_ready = not work_blockers

    cash_blockers: list[str] = []
    if not market.payout_ready:
        cash_blockers.append("PAYOUT_NOT_READY")
    if not market.south_africa_verified:
        cash_blockers.append("SOUTH_AFRICA_NOT_VERIFIED")
    cash_ready = not cash_blockers

    proving_blockers = list(work_blockers)
    if not market.enabled:
        proving_blockers.append("MARKET_DISABLED")
    if market.status != Marketplace.Status.LIVE:
        proving_blockers.append("MARKET_NOT_LIVE")
    platform_wallet_proving = market.slug in PLATFORM_WALLET_PROVING_MARKETS
    if not cash_ready and not platform_wallet_proving:
        proving_blockers.append("CASH_ROUTE_REQUIRED_FOR_LIVE_PROVING")
    proving_blockers = list(dict.fromkeys(proving_blockers))
    live_test_ready = not proving_blockers

    switch_name = _auto_switch_name(market.slug)
    runtime_switch = os.getenv(switch_name, "0") == "1"
    profile_switch = bool(profile and profile.autonomous_acquisition_enabled)
    mode = current_mode().value
    autonomy_blockers = list(proving_blockers)
    if mode not in {"LOW_RISK", "FULL"}:
        autonomy_blockers.append("AUTONOMY_NOT_MUTATING")
    if not runtime_switch:
        autonomy_blockers.append("ACQUISITION_RUNTIME_SWITCH_DISABLED")
    if not profile_switch:
        autonomy_blockers.append("MARKET_AUTONOMOUS_ACQUISITION_DISABLED")
    autonomy_blockers = list(dict.fromkeys(autonomy_blockers))

    return {
        "market": market.slug,
        "work_ready": work_ready,
        "work_blockers": work_blockers,
        "live_test_ready": live_test_ready,
        "live_test_blockers": proving_blockers,
        "platform_wallet_proving": platform_wallet_proving,
        "cash_ready": cash_ready,
        "cash_blockers": cash_blockers,
        "autonomy_ready": not autonomy_blockers,
        "autonomy_blockers": autonomy_blockers,
        "autonomy_mode": mode,
        "runtime_switch_name": switch_name,
        "runtime_switch_enabled": runtime_switch,
        "profile_acquisition_enabled": profile_switch,
        "connected": connected,
        "source_wired": source_wired,
        "policy_current": policy_current,
        "policy_verified": policy_verified,
        "truth": (
            "Work readiness, live proving and cash readiness are separate. "
            "Platform-wallet proving never counts as settled cash and never opens Banking."
        ),
    }
