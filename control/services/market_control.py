from __future__ import annotations

from copy import deepcopy
from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, MarketIntegrationProfile, Marketplace
from control.services.market_readiness import market_readiness


OPERATOR_PROOF_KEY = "operator_proof"
DEALWORK_KYA_PROOF_KEY = "dealwork_kya"
DEALWORK_KYA_BLOCKER = "DEALWORK_KYA_NOT_VERIFIED"


def _clean_reference(value: str) -> str:
    return str(value or "").strip()[:255]


def overlay_operator_profile_truth(
    market_slug: str,
    *,
    base_evidence: dict | None,
    base_blockers: list | tuple | None,
    existing_evidence: dict | None,
) -> tuple[dict, list[str]]:
    """Preserve owner proof when static market catalog truth is refreshed.

    Only explicitly managed proof can suppress an explicitly managed blocker.
    Unknown/operator-unmanaged blockers always survive from the static catalog.
    """
    evidence = deepcopy(base_evidence) if isinstance(base_evidence, dict) else {}
    existing = existing_evidence if isinstance(existing_evidence, dict) else {}
    operator_proof = deepcopy(existing.get(OPERATOR_PROOF_KEY)) if isinstance(existing.get(OPERATOR_PROOF_KEY), dict) else {}
    if operator_proof:
        evidence[OPERATOR_PROOF_KEY] = operator_proof

    blockers = [str(code) for code in (base_blockers or [])]
    if market_slug == "dealwork":
        proof = operator_proof.get(DEALWORK_KYA_PROOF_KEY) if isinstance(operator_proof, dict) else None
        proof = proof if isinstance(proof, dict) else {}
        if proof.get("verified") is True and _clean_reference(proof.get("proof_reference")):
            blockers = [code for code in blockers if code != DEALWORK_KYA_BLOCKER]
    return evidence, list(dict.fromkeys(blockers))


def _profile_proof(profile: MarketIntegrationProfile) -> dict:
    evidence = profile.evidence if isinstance(profile.evidence, dict) else {}
    operator = evidence.get(OPERATOR_PROOF_KEY) if isinstance(evidence.get(OPERATOR_PROOF_KEY), dict) else {}
    proof = operator.get(DEALWORK_KYA_PROOF_KEY) if isinstance(operator, dict) else {}
    return proof if isinstance(proof, dict) else {}


def market_control_row(market: Marketplace) -> dict:
    try:
        profile = market.integration_profile
    except Exception:
        profile = None
    readiness = market_readiness(market)
    proof = _profile_proof(profile) if profile is not None and market.slug == "dealwork" else {}
    return {
        **readiness,
        "display_name": market.display_name,
        "enabled": bool(market.enabled),
        "status": market.status,
        "payout_ready": bool(market.payout_ready),
        "south_africa_verified": bool(market.south_africa_verified),
        "kya_supported": market.slug == "dealwork",
        "kya_verified": bool(proof.get("verified")),
        "kya_proof_reference": _clean_reference(proof.get("proof_reference")),
        "kya_verified_at": str(proof.get("verified_at") or ""),
        "can_activate": bool(readiness["live_entry_ready"]),
        "can_arm_persisted_acquisition": bool(
            readiness["live_entry_ready"] and market.enabled and market.status == Marketplace.Status.LIVE
        ),
        "runtime_configuration_mutable_here": False,
        "runtime_configuration_note": (
            "AUTONOMOUS_MODE and per-market runtime environment switches are deployment-controlled and read-only here."
        ),
    }


def market_controls_snapshot() -> dict:
    rows = [market_control_row(market) for market in Marketplace.objects.order_by("slug")]
    return {
        "section": "market-controls",
        "rows": rows,
        "meta": {
            "work_ready": sum(1 for row in rows if row["work_ready"]),
            "live_test_ready": sum(1 for row in rows if row["live_test_ready"]),
            "cash_ready": sum(1 for row in rows if row["cash_ready"]),
            "autonomy_ready": sum(1 for row in rows if row["autonomy_ready"]),
            "truth": (
                "Work readiness, live proving, cash settlement and autonomous mutation are independent gates. "
                "Platform-wallet proving never counts as settled cash."
            ),
        },
    }


@transaction.atomic
def update_market_compliance_proof(
    market_slug: str,
    *,
    proof_type: str,
    verified: bool,
    proof_reference: str,
    actor: str,
) -> dict:
    market = Marketplace.objects.select_for_update().get(slug=market_slug)
    profile = MarketIntegrationProfile.objects.select_for_update().get(marketplace=market)
    if market.slug != "dealwork" or str(proof_type or "").strip().lower() != "kya":
        raise ValueError("unsupported_market_proof")

    reference = _clean_reference(proof_reference)
    if verified and not reference:
        raise ValueError("non_secret_proof_reference_required")

    evidence = deepcopy(profile.evidence) if isinstance(profile.evidence, dict) else {}
    operator = deepcopy(evidence.get(OPERATOR_PROOF_KEY)) if isinstance(evidence.get(OPERATOR_PROOF_KEY), dict) else {}
    operator[DEALWORK_KYA_PROOF_KEY] = {
        "verified": bool(verified),
        "proof_reference": reference if verified else "",
        "verified_at": timezone.now().isoformat() if verified else "",
        "actor": str(actor),
    }
    evidence[OPERATOR_PROOF_KEY] = operator

    blockers = [str(code) for code in (profile.blockers or []) if str(code)]
    if verified:
        blockers = [code for code in blockers if code != DEALWORK_KYA_BLOCKER]
    elif DEALWORK_KYA_BLOCKER not in blockers:
        blockers.append(DEALWORK_KYA_BLOCKER)

    profile.evidence = evidence
    profile.blockers = list(dict.fromkeys(blockers))
    profile_updates = ["evidence", "blockers", "updated_at"]

    if not verified and profile.autonomous_acquisition_enabled:
        profile.autonomous_acquisition_enabled = False
        profile_updates.append("autonomous_acquisition_enabled")
    profile.save(update_fields=profile_updates)

    market_updates: list[str] = []
    if not verified:
        if market.enabled:
            market.enabled = False
            market_updates.append("enabled")
        if market.status == Marketplace.Status.LIVE:
            market.status = Marketplace.Status.WATCH_ONLY
            market_updates.append("status")
        if market_updates:
            market.save(update_fields=[*market_updates, "updated_at"])

    AuditEvent.objects.create(
        severity="INFO" if verified else "WARN",
        event_type="market.compliance_proof_updated",
        actor=str(actor),
        metadata={
            "market": market.slug,
            "proof_type": "KYA",
            "verified": bool(verified),
            "proof_reference_present": bool(reference if verified else ""),
            "market_disarmed": not verified,
        },
    )
    market.refresh_from_db()
    return market_control_row(market)


@transaction.atomic
def update_market_operating_state(
    market_slug: str,
    *,
    enabled: bool,
    autonomous_acquisition_enabled: bool,
    actor: str,
) -> dict:
    market = Marketplace.objects.select_for_update().get(slug=market_slug)
    profile = MarketIntegrationProfile.objects.select_for_update().get(marketplace=market)

    before_payout = bool(market.payout_ready)
    before_sa = bool(market.south_africa_verified)

    readiness = market_readiness(market)
    if enabled and not readiness["live_entry_ready"]:
        raise ValueError("market_live_entry_not_ready:" + ",".join(readiness["live_entry_blockers"]))

    if enabled:
        market.enabled = True
        market.status = Marketplace.Status.LIVE
    else:
        market.enabled = False
        market.status = Marketplace.Status.WATCH_ONLY
        autonomous_acquisition_enabled = False

    if autonomous_acquisition_enabled:
        # The environment/global autonomy switches remain independent and may stay OFF.
        if not enabled:
            raise ValueError("market_must_be_live_before_acquisition_arm")
        readiness_after_entry = market_readiness(market)
        if not readiness_after_entry["live_entry_ready"]:
            raise ValueError("market_live_entry_not_ready:" + ",".join(readiness_after_entry["live_entry_blockers"]))

    profile.autonomous_acquisition_enabled = bool(autonomous_acquisition_enabled)
    market.save(update_fields=["enabled", "status", "updated_at"])
    profile.save(update_fields=["autonomous_acquisition_enabled", "updated_at"])

    # This endpoint is operational only. Banking truth must never be mutated here.
    market.refresh_from_db()
    if bool(market.payout_ready) != before_payout or bool(market.south_africa_verified) != before_sa:
        raise RuntimeError("market_control_must_not_mutate_banking_truth")

    AuditEvent.objects.create(
        event_type="market.operating_state_updated",
        actor=str(actor),
        metadata={
            "market": market.slug,
            "enabled": bool(market.enabled),
            "status": market.status,
            "persisted_acquisition_enabled": bool(profile.autonomous_acquisition_enabled),
            "runtime_switch_mutated": False,
            "banking_truth_mutated": False,
        },
    )
    return market_control_row(market)
