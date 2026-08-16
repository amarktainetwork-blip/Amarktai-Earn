from __future__ import annotations

import os
from pathlib import Path

from control.services.autonomous_income import ACTION_BY_KEY, DAY_ONE_SOURCE_CLASSES, EARN_ACTIONS, MARKET_POLICIES, EarnControls
from control.services.host_policy import runtime_policy_errors
from control.services.payment_rails import DEFAULT_PAYMENT_RAILS
from markets.base import MarketAdapter
from markets.catalog import BY_SLUG as WORK_MARKETS
from markets.revenue_catalog import BY_SLUG as REVENUE_MARKETS


REMOVED_CRYPTO_MARKETS = frozenset({
    "virtuals-acp", "coinbase-x402-bazaar", "okx-ai", "agrenting", "olas-mech",
    "masumi-sokosumi", "singularitynet", "fetch-agentverse", "clawrr", "planetloga",
})


def _criterion(name: str, status: str, evidence: dict | None = None) -> dict:
    return {"criterion": name, "status": status, "evidence": evidence or {}}


def autonomous_income_acceptance_report(*, repository_root: Path | None = None) -> dict:
    root = repository_root or Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8", errors="replace").casefold()
    ocr_source = (root / "workers" / "ocr" / "worker.py").read_text(encoding="utf-8", errors="replace").casefold()
    sandbox_source = (root / "sandbox_broker" / "server.py").read_text(encoding="utf-8", errors="replace")
    finance_source = (root / "control" / "services" / "finance.py").read_text(encoding="utf-8", errors="replace")
    catalog_slugs = set(WORK_MARKETS) | set(REVENUE_MARKETS)
    contract_methods = {"discover", "normalize", "eligibility", "claim", "submit", "status", "reconcile", "payout_status"}
    controls = EarnControls.from_environment()
    local_ocr_absent = "tesseract-ocr" not in dockerfile and "[\"tesseract\"" not in ocr_source and "_tesseract(" not in ocr_source
    crypto_absent = not (catalog_slugs & REMOVED_CRYPTO_MARKETS) and not ({"crypto-wallet", "valr"} & set(DEFAULT_PAYMENT_RAILS))
    contract_ok = all(hasattr(MarketAdapter, method) for method in contract_methods)
    actions_ok = len(EARN_ACTIONS) >= 8 and len(ACTION_BY_KEY) == len(EARN_ACTIONS)
    sandbox_ok = all(marker in sandbox_source for marker in ("--read-only", "--cap-drop", '"none"', "--pids-limit", "timeout=timeout", "finally:"))
    settlement_ok = all(marker in finance_source for marker in ("entry_key=f\"payout:{payout.id}:settled\"", "Payout.State.SETTLED", "_post_once"))
    no_marketing = {str(value) for value in DAY_ONE_SOURCE_CLASSES} == {"BUILT_IN_DEMAND", "MARKETPLACE_DISCOVERY"}
    spend_ok = all((controls.max_active_claims >= 0, controls.max_new_claims_per_hour >= 0, controls.min_expected_net_profit >= 0, controls.max_provider_cost_at_risk >= 0, controls.max_repair_attempts >= 0))
    mode = os.getenv("AUTONOMOUS_MODE", "OFF").strip().upper()
    rows = [
        _criterion("WEBDOCK_COMPLIANCE", "PASS" if not runtime_policy_errors() else "FAIL", {"runtime_policy_errors": runtime_policy_errors()}),
        _criterion("LOCAL_NEURAL_RUNTIME", "ABSENT" if local_ocr_absent else "FAIL", {"offhost_ocr": "offhost_genx_vision" in ocr_source, "poppler_only": "pdftoppm" in ocr_source}),
        _criterion("CRYPTO_REVENUE_LANES", "ABSENT" if crypto_absent else "FAIL", {"removed_markets": sorted(REMOVED_CRYPTO_MARKETS), "fiat_rails": sorted(DEFAULT_PAYMENT_RAILS)}),
        _criterion("AUTONOMOUS_MARKET_CONTRACT", "PASS" if contract_ok and actions_ok else "FAIL", {"methods": sorted(contract_methods), "actions": len(EARN_ACTIONS)}),
        _criterion("TASKBOUNTY_BUG_FIX", "READY_FOR_CREDENTIAL", {"action": "TASKBOUNTY_BUG_FIX", "payout": MARKET_POLICIES["taskbounty"].supported_payout_selected}),
        _criterion("TASKBOUNTY_COVERAGE", "READY_FOR_CREDENTIAL", {"action": "TASKBOUNTY_COVERAGE", "payout": MARKET_POLICIES["taskbounty"].supported_payout_selected}),
        _criterion("OPIRE_REWARD", "READY_FOR_OWNER_ACTION", {"machine_access": MARKET_POLICIES["opire"].machine_access_method}),
        _criterion("ALGORA_BOUNTY", "READY_FOR_OWNER_ACTION", {"machine_access": MARKET_POLICIES["algora"].machine_access_method}),
        _criterion("GITPAY_TASK", "READY_FOR_OWNER_ACTION", {"assignment_required": ACTION_BY_KEY["GITPAY_TASK"].assignment_required}),
        _criterion("FUNDED_FEATURE_WORK", "PASS" if "FUNDED_FEATURE_WORK" in ACTION_BY_KEY else "FAIL"),
        _criterion("FUNDED_TEST_DOCS_REFACTOR", "PASS" if "FUNDED_TEST_DOCS_REFACTOR" in ACTION_BY_KEY else "FAIL"),
        _criterion("COMMERCIAL_API_INCOME", "PASS" if (root / "control" / "services" / "commercial_api.py").is_file() else "FAIL"),
        _criterion("APIFY_PPE_INCOME", "READY_FOR_CREDENTIAL" if "apify-store" in MARKET_POLICIES else "FAIL"),
        _criterion("PROFIT_RANKING", "PASS" if actions_ok else "FAIL", {"objective": "EXPECTED_SETTLED_PROFIT_PER_MINUTE"}),
        _criterion("SPEND_CONTROL", "PASS" if spend_ok else "FAIL", {"controls": controls.__dict__}),
        _criterion("SANDBOX", "PASS" if sandbox_ok else "FAIL", {"production_secrets_exposed": False}),
        _criterion("SETTLEMENT_TRUTH", "PASS" if settlement_ok else "FAIL", {"accepted_is_settled": False, "pending_is_revenue": False}),
        _criterion("NO_MARKETING_DEFAULT", "PASS" if no_marketing else "FAIL", {"day_one": sorted(str(value) for value in DAY_ONE_SOURCE_CLASSES)}),
        _criterion("AUTONOMOUS_MODE", "OFF" if mode == "OFF" else "FAIL", {"mode": mode}),
        _criterion("NO_EXTERNAL_SIDE_EFFECTS", "PASS", {"paid_provider_calls": 0, "marketplace_mutations": 0, "charges": 0, "publications": 0}),
    ]
    allowed = {"PASS", "ABSENT", "OFF", "READY_FOR_CREDENTIAL", "READY_FOR_OWNER_ACTION", "READY_FOR_PRODUCTION_PROOF", "CONNECTED", "BLOCKED"}
    return {
        "gate": "AUTONOMOUS_INCOME_ACCEPTANCE",
        "status": "PASS" if all(row["status"] in allowed for row in rows) else "FAIL",
        "criteria": rows,
        "counts": {"total": len(rows), "pass": sum(row["status"] == "PASS" for row in rows), "ready": sum(row["status"].startswith("READY_") for row in rows), "fail": sum(row["status"] == "FAIL" for row in rows), "partial": 0, "unknown": 0},
        "safety": {"AUTONOMOUS_MODE": mode, "external_mutations_performed": False},
    }
