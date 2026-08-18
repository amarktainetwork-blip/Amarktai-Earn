from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from control.services.integration_accounts import integration_accounts_snapshot
from control.services.phase2_acceptance import phase2_acceptance_report
from control.services.prelive_acceptance import prelive_acceptance_report
from control.services.self_improvement import self_improvement_contract
from control.services.v1_acceptance import build_acceptance_report


PASS = "PASS"
READY_CREDENTIAL = "READY_FOR_CREDENTIAL"
READY_OWNER = "READY_FOR_OWNER_ACTION"
READY_PRODUCTION = "READY_FOR_PRODUCTION_PROOF"
FAIL = "FAIL"


def _prelive_index(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["name"]): row for row in report.get("criteria", [])}


def _v1_index(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["id"]): row for row in report.get("criteria", [])}


def _all_pass(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and all(str(row.get("status")) == PASS for row in rows)


def _prelive_group(index: dict[str, dict[str, Any]], names: tuple[str, ...]) -> dict[str, Any]:
    rows = [index[name] for name in names if name in index]
    missing = [name for name in names if name not in index]
    failed = [row["name"] for row in rows if row.get("status") != PASS]
    return {
        "status": PASS if not missing and not failed else FAIL,
        "missing": missing,
        "failed": failed,
        "evidence": [row.get("evidence", "") for row in rows],
    }


def _v1_group(index: dict[str, dict[str, Any]], ids: tuple[str, ...]) -> dict[str, Any]:
    rows = [index[identifier] for identifier in ids if identifier in index]
    missing = [identifier for identifier in ids if identifier not in index]
    code_failures = [row["id"] for row in rows if row.get("status") == FAIL]
    return {
        "status": PASS if not missing and not code_failures else FAIL,
        "missing": missing,
        "code_failures": code_failures,
        "non_code_proof": [
            {"id": row["id"], "status": row["status"], "operator_action": row.get("operator_action", "")}
            for row in rows
            if row.get("status") in {"BLOCKED", "EXTERNAL_PROOF_REQUIRED"}
        ],
    }


def _phase2_family(report: dict[str, Any], family: str) -> dict[str, Any]:
    rows = [row for row in report.get("rows", []) if row.get("family") == family and row.get("kind") in {"operation", "workflow", "external_tool", "runtime"}]
    failures = [row["name"] for row in rows if row.get("status") == FAIL]
    return {
        "status": PASS if rows and not failures else FAIL,
        "failures": failures,
        "pass": sum(row.get("status") == PASS for row in rows),
        "ready_for_credential": sum(row.get("status") == READY_CREDENTIAL for row in rows),
    }


def _connection_rows() -> list[dict[str, Any]]:
    rows = []
    for row in integration_accounts_snapshot().get("rows", []):
        credential_fields = row.get("credential_fields") or []
        connected = bool(row.get("connected"))
        autonomy_ready = bool(row.get("autonomy_ready"))
        if connected:
            status = PASS
        elif any(field.get("required") for field in credential_fields):
            status = READY_CREDENTIAL
        else:
            status = READY_OWNER
        rows.append({
            "slug": row.get("slug"),
            "name": row.get("display_name"),
            "category": row.get("category"),
            "status": status,
            "setup_state": row.get("setup_state"),
            "connected": connected,
            "autonomy_ready": autonomy_ready,
            "connection_test_mode": row.get("connection_test_mode"),
            "owner_action": row.get("owner_action_required"),
            "payout_route": row.get("payout_route"),
        })
    return rows


def phase3_acceptance_report(*, ci_proven: bool = False, repository_root: Path | None = None) -> dict[str, Any]:
    root = repository_root or Path(__file__).resolve().parents[2]
    phase2 = phase2_acceptance_report(repository_root=root)
    prelive = prelive_acceptance_report()
    v1 = build_acceptance_report(ci_proven=ci_proven)
    self_building = self_improvement_contract()
    pindex = _prelive_index(prelive)
    vindex = _v1_index(v1)

    core = []

    def add(name: str, status: str, details: Any):
        core.append({"name": name, "status": status, "details": details})

    add("SYSTEM", PASS if prelive.get("result") == PASS and v1.get("counts", {}).get(FAIL, 0) == 0 else FAIL, {
        "prelive_result": prelive.get("result"),
        "v1_overall": v1.get("overall_status"),
        "v1_fail_count": v1.get("counts", {}).get(FAIL, 0),
    })
    add("CAPABILITIES", PASS if phase2.get("status") == PASS else FAIL, phase2.get("summary", {}))
    add("ARTIFACTS", _v1_group(vindex, ("multifile_composite", "expanded_worker_qa"))["status"], _v1_group(vindex, ("multifile_composite", "expanded_worker_qa")))
    add("PROVIDER_CONTRACTS", _prelive_group(pindex, (
        "GenX live catalogue synchronization", "GenX actual billing and monetary cost truth",
        "Dimensionally valid task-specific routing", "All paid paths use economic routing",
    ))["status"], _prelive_group(pindex, (
        "GenX live catalogue synchronization", "GenX actual billing and monetary cost truth",
        "Dimensionally valid task-specific routing", "All paid paths use economic routing",
    )))
    add("TOOL_CONTRACTS", PASS if _phase2_family(phase2, "web_browser")["status"] == PASS else FAIL, _phase2_family(phase2, "web_browser"))
    add("BROWSER", PASS if _phase2_family(phase2, "web_browser")["status"] == PASS else FAIL, _phase2_family(phase2, "web_browser"))
    add("SCRAPING", PASS if _phase2_family(phase2, "web_browser")["status"] == PASS else FAIL, _phase2_family(phase2, "web_browser"))
    add("DOCUMENTS", _phase2_family(phase2, "documents")["status"], _phase2_family(phase2, "documents"))
    add("CODE", _phase2_family(phase2, "code_software")["status"], _phase2_family(phase2, "code_software"))
    media_status = PASS if all(_phase2_family(phase2, family)["status"] == PASS for family in ("audio", "video", "media_generation")) else FAIL
    add("MEDIA", media_status, {family: _phase2_family(phase2, family) for family in ("audio", "video", "media_generation")})

    product = _prelive_group(pindex, (
        "Product Factory lanes", "Budgets, inventory and stop-loss", "Capability monetization matrix",
        "Publication-ready inventory", "Authoritative ROI learning",
    ))
    add("PRODUCT_FACTORY", product["status"], product)
    profit = _v1_group(vindex, ("growth_governor", "uncapped_profit_governor", "utilization_economics", "adaptive_economic_learning", "seller_pricing_profit_floor"))
    add("PROFIT_BRAIN", profit["status"], profit)
    order = _v1_group(vindex, ("inbound_order_uses_canonical_job_lifecycle", "global_portfolio_ranking", "acquisition_gates"))
    add("ORDER_PIPELINE", order["status"], order)
    delivery = _v1_group(vindex, ("qa_repair", "lifecycle_logic", "only_settled_is_cash"))
    add("DELIVERY", delivery["status"], delivery)
    payment = _prelive_group(pindex, ("Paystack charge is not settlement", "Paystack Settlement API truth", "External event idempotency"))
    add("PAYMENT_PIPELINE", payment["status"], payment)
    accounting = _prelive_group(pindex, ("GenX actual billing and monetary cost truth", "Authoritative ROI learning"))
    add("ACCOUNTING", PASS if accounting["status"] == PASS and _v1_group(vindex, ("money_truth", "only_settled_is_cash"))["status"] == PASS else FAIL, {
        "prelive": accounting,
        "v1": _v1_group(vindex, ("money_truth", "only_settled_is_cash")),
    })
    markets = _prelive_group(pindex, ("20-account canonical catalogue", "Connection test or manual boundary", "Priority channels", "Bounded credential-aware cycle"))
    add("MARKET_CONNECTORS", markets["status"], markets)
    banking = _prelive_group(pindex, ("20-account canonical catalogue", "Write-only encrypted credentials", "No bank or wallet private-key schema", "Revocation fail-closed"))
    add("BANKING", banking["status"], banking)
    add("SELF_BUILDING", PASS if self_building.get("status") == PASS and not self_building.get("production_self_merge") and not self_building.get("production_self_deploy") else FAIL, self_building)
    autonomy = str(os.getenv("AUTONOMOUS_MODE", "OFF")).upper()
    add("AUTONOMY_GUARD", PASS if autonomy == "OFF" else FAIL, {"mode": autonomy, "required_before_live_proof": "OFF"})

    core_failures = [row for row in core if row["status"] == FAIL]
    connections = _connection_rows()
    external_v1 = [
        {
            "id": row["id"],
            "title": row["title"],
            "status": READY_PRODUCTION if row["status"] == "EXTERNAL_PROOF_REQUIRED" else READY_OWNER,
            "operator_action": row.get("operator_action", ""),
        }
        for row in v1.get("criteria", [])
        if row.get("status") in {"BLOCKED", "EXTERNAL_PROOF_REQUIRED"}
    ]
    summary = {
        "CORE_TOTAL": len(core),
        "CORE_PASS": len(core) - len(core_failures),
        "FAILURES": len(core_failures),
        "PARTIAL": 0,
        "UNKNOWN": 0,
        "CONNECTIONS_TOTAL": len(connections),
        "CONNECTIONS_CONNECTED": sum(row["status"] == PASS for row in connections),
        "CONNECTIONS_READY_FOR_CREDENTIAL": sum(row["status"] == READY_CREDENTIAL for row in connections),
        "CONNECTIONS_READY_FOR_OWNER_ACTION": sum(row["status"] == READY_OWNER for row in connections),
        "EXTERNAL_PRODUCTION_PROOFS": len(external_v1),
    }
    return {
        "phase": 3,
        "name": "MONEY_MAKING_SYSTEM_READY_FOR_KEYS",
        "status": PASS if not core_failures else FAIL,
        "engineering_ready_for_keys": not core_failures,
        "autonomy_enabled": autonomy != "OFF",
        "summary": summary,
        "core": core,
        "external_connections": connections,
        "external_production_proofs": external_v1,
        "phase2": {
            "status": phase2.get("status"),
            "summary": phase2.get("summary"),
        },
        "prelive": {
            "result": prelive.get("result"),
            "code_blocker_count": prelive.get("code_blocker_count"),
        },
        "v1": {
            "overall_status": v1.get("overall_status"),
            "counts": v1.get("counts"),
            "ci_proven_context": v1.get("ci_proven_context"),
        },
        "note": "PASS means engineering is ready for credential and production-proof activation; it does not claim external account setup, live provider proof, physical reboot, a funded order, or settled cash.",
    }
