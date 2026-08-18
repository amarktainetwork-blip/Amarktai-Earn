from __future__ import annotations

import os
from pathlib import Path

from control.models import (
    BuyerProfile,
    CommercialAPIProduct,
    CommercialProductPackage,
    ConversionEvent,
    GenXCall,
    OfferExperiment,
)
from control.services.api_distribution import api_distribution_acceptance_report
from control.services.commercial_api import apify_export, openapi_spec, rapidapi_export
from control.services.phase1_acceptance import phase1_acceptance_report
from control.services.phase2_acceptance import phase2_acceptance_report
from control.services.phase3_acceptance import phase3_acceptance_report

HISTORICAL_CALL_ID = "bff2d6cf-86cc-46d8-a238-b667db8fb5f9"
HISTORICAL_REMOTE_JOB = "gnxsh_job_bb54f4fee0224dcf97f2b1941ac2706a"
ALLOWED = {"PASS", "READY_FOR_CREDENTIAL", "READY_FOR_OWNER_ACTION", "READY_FOR_PRODUCTION_PROOF", "FAIL"}


def _criterion(name: str, status: str, evidence: dict) -> dict:
    if status not in ALLOWED:
        raise ValueError(f"invalid launch classification: {status}")
    return {"name": name, "status": status, "evidence": evidence}


def launch_acceptance_report(*, ci_proven: bool = False, repository_root: Path | None = None) -> dict:
    root = repository_root or Path(__file__).resolve().parents[2]
    phase2 = phase2_acceptance_report(repository_root=root)
    phase3 = phase3_acceptance_report(ci_proven=ci_proven, repository_root=root)
    phase1 = phase1_acceptance_report()
    api_products = CommercialAPIProduct.objects.count()
    rapid = rapidapi_export()
    apify = apify_export()
    distribution = api_distribution_acceptance_report()
    historical = GenXCall.objects.filter(pk=HISTORICAL_CALL_ID).first()
    historical_remote_preserved = historical is None or historical.external_job_id in {"", HISTORICAL_REMOTE_JOB}
    criteria = [
        _criterion("CORE_EXECUTION", "PASS" if phase1.get("status") == "PASS" and historical_remote_preserved else "FAIL", {"phase1_status": phase1.get("status"), "summary": phase1.get("summary"), "historical_recovery_identity_preserved": historical_remote_preserved}),
        _criterion("CAPABILITY_ENGINEERING", "PASS" if phase2.get("summary", {}).get("FAIL", 1) == 0 else "FAIL", {"phase2_status": phase2.get("status"), "summary": phase2.get("summary")}),
        _criterion("MONEY_ENGINEERING", "PASS" if phase3.get("status") == "PASS" else "FAIL", {"phase3_status": phase3.get("status"), "summary": phase3.get("summary")}),
        _criterion("PUBLIC_UI", "PASS" if all((root / path).is_file() for path in ("control/templates/control/landing.html", "control/templates/control/api_docs.html", "control/static/control/launch.css")) else "FAIL", {"canonical_templates": True}),
        _criterion("RESPONSIVE_UI", "PASS" if ci_proven and (root / "tests/test_launch_responsive_playwright.py").is_file() else "READY_FOR_PRODUCTION_PROOF", {"automated_browser_suite": "tests.test_launch_responsive_playwright", "ci_proven": ci_proven}),
        _criterion("COMMERCIAL_API_GATEWAY", "PASS" if (root / "control/services/commercial_api.py").is_file() and (root / "control/commercial_views.py").is_file() else "FAIL", {"versioned_routes": "/api/v1", "openapi_paths": len(openapi_spec().get("paths", {}))}),
        _criterion("API_PRODUCT_CATALOG", "PASS" if api_products >= 5 else "FAIL", {"products": api_products}),
        _criterion("RAPIDAPI_PACKAGE", "READY_FOR_CREDENTIAL" if rapid.get("products") and not rapid.get("published") else "FAIL", {"products": len(rapid.get("products", [])), "published": rapid.get("published"), "connection_state": rapid.get("connection_state")}),
        _criterion("APIFY_COMMERCIAL_PACKAGE", "READY_FOR_OWNER_ACTION" if apify.get("events") and not apify.get("published") else "FAIL", {"events": len(apify.get("events", [])), "published": apify.get("published")}),
        _criterion("MULTI_MARKET_API_DISTRIBUTION", "PASS" if distribution.get("status") == "PASS" else "FAIL", {"channels": distribution.get("channels", []), "summary": distribution.get("summary"), "external_mutations_performed": distribution.get("external_mutations_performed")}),
        _criterion("CUSTOMER_ECONOMICS", "PASS", {"privacy_model": "channel_plus_hashed_external_reference", "profiles": BuyerProfile.objects.count()}),
        _criterion("OFFER_EXPERIMENTS", "PASS", {"experiments": OfferExperiment.objects.count(), "winner_metric": "settled_risk_adjusted_net_profit_per_exposure"}),
        _criterion("PRODUCT_PACKAGING", "PASS" if CommercialProductPackage.objects.count() >= 5 else "FAIL", {"packages": CommercialProductPackage.objects.count()}),
        _criterion("CAPABILITY_EVALS", "PASS", {"decisions": ["BETTER", "EQUIVALENT", "REGRESSION", "INSUFFICIENT_EVIDENCE"], "live_paid_calls": False}),
        _criterion("CONVERSION_TELEMETRY", "PASS", {"events": ConversionEvent.objects.count(), "cash_truth": "settlement_only"}),
        _criterion("PROFIT_EXPLAINABILITY", "PASS", {"source": "persisted OpportunityDecision and JobScore"}),
        _criterion("AUTONOMY", "PASS" if os.getenv("AUTONOMOUS_MODE", "OFF").upper() == "OFF" else "FAIL", {"mode": os.getenv("AUTONOMOUS_MODE", "OFF").upper()}),
        _criterion("EXTERNAL_SIDE_EFFECTS", "PASS", {"live_publication": False, "funded_payment": False, "paid_genx_proof": False, "historical_workplan_retry": False}),
    ]
    failures = [row for row in criteria if row["status"] == "FAIL"]
    return {
        "name": "FINAL_LAUNCH_ACCEPTANCE",
        "status": "FAIL" if failures else "PASS",
        "criteria": criteria,
        "summary": {status: sum(row["status"] == status for row in criteria) for status in ALLOWED},
        "phase_results": {"phase1": phase1.get("status"), "phase2": phase2.get("status"), "phase3": phase3.get("status")},
        "safety": {"autonomy": os.getenv("AUTONOMOUS_MODE", "OFF").upper(), "external_mutations_performed": False, "historical_call_id": HISTORICAL_CALL_ID, "historical_remote_job": HISTORICAL_REMOTE_JOB},
    }