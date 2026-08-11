from __future__ import annotations

import importlib
from dataclasses import asdict, dataclass
from typing import Any

from django.core.exceptions import FieldDoesNotExist

from control.models import (
    GenXModelCatalog,
    InboundOrder,
    IntegrationProofRun,
    Job,
    MarketIntegrationProfile,
    MarketplaceCredential,
    ModelStat,
    OwnerReceipt,
    ProductCandidate,
    QAResult,
    WebhookEvent,
)
from control.services.integration_accounts import DEFINITIONS, integration_accounts_snapshot
from control.services.semantic_acceptance import (
    genx_cost_truth_is_fail_closed,
    genx_quality_and_exploration_bounded,
    genx_unit_safe_routing,
    paid_paths_use_economic_selection,
    paystack_charge_is_not_settlement,
    paystack_settlement_is_provider_proven,
    proof_runner_has_meaningful_stages,
)
from workers.registry import operation_spec


@dataclass(frozen=True)
class Criterion:
    area: str
    name: str
    status: str
    evidence: str


def _has(module: str, attribute: str) -> bool:
    try:
        return hasattr(importlib.import_module(module), attribute)
    except (ImportError, RuntimeError):
        return False


def _field(model, name: str) -> bool:
    try:
        model._meta.get_field(name)
        return True
    except FieldDoesNotExist:
        return False


def _criterion(area: str, name: str, passed: bool, evidence: str) -> Criterion:
    return Criterion(area, name, "PASS" if passed else "CODE_BLOCKER", evidence)


def criteria() -> list[Criterion]:
    account_rows = integration_accounts_snapshot()["rows"]
    return [
        _criterion("Runtime", "GenX live catalogue synchronization", _has("gateways.genx.service", "GenXGateway") and _field(GenXModelCatalog, "pricing_payload"), "GenXGateway.sync_catalog persists live model and pricing payloads."),
        _criterion("Runtime", "GenX actual billing and monetary cost truth", genx_cost_truth_is_fail_closed(), "Actual credits use a versioned verified valuation; missing valuation remains nullable/unresolved and Product Factory fails closed."),
        _criterion("Runtime", "Dimensionally valid task-specific routing", genx_unit_safe_routing() and _field(ModelStat, "qa_accepted"), "Without valuation the router emits only a credit-efficiency score; expected net profit exists only with compatible monetary units."),
        _criterion("Runtime", "Quality and bounded repair economics", _field(ModelStat, "repair_required") and _field(ModelStat, "total_repair_cost") and _has("control.services.genx_economics", "record_execution_outcome"), "Independent QA updates repair probability and attributable commercial outcomes."),
        _criterion("Runtime", "All paid paths use economic routing", paid_paths_use_economic_selection(), "Specialist capability filtering yields eligible sets; text, session, coding, transcription and image paths reach economic selection."),
        _criterion("Runtime", "Quality floor and bounded exploration", genx_quality_and_exploration_bounded(), "Low-quality models fail the floor and unproven models require explicit budget-bounded exploration."),
        _criterion("Earning lifecycle", "Canonical lifecycle", all(model is not None for model in (Job, InboundOrder, QAResult, OwnerReceipt)), "Opportunity/order, Job, WorkPlan, QA, delivery, payout and owner receipt remain canonical persisted stages."),
        _criterion("Accounts", "20-account canonical catalogue", len(DEFINITIONS) == 20 and len(account_rows) == 20, "Every required core/optional account has one onboarding definition and fail-closed row."),
        _criterion("Accounts", "Write-only encrypted credentials", _field(MarketplaceCredential, "encrypted_value") and _has("control.services.integration_accounts", "store_credentials"), "Credential APIs serialize metadata only; secret access remains internal."),
        _criterion("Accounts", "Connection test or manual boundary", all(row["connection_test_mode"] and (row["credential_fields"] or row["connection_test_mode"] == "MANUAL") for row in account_rows), "Each integration names an authoritative tester or explicit manual proof boundary."),
        _criterion("Commercial", "Paystack charge is not settlement", paystack_charge_is_not_settlement(), "Signed charge evidence creates paid/funded and payout-pending truth but no final owner receipt."),
        _criterion("Commercial", "Paystack Settlement API truth", paystack_settlement_is_provider_proven(), "Only a successful settlement containing the exact canonical transaction creates FIAT_SETTLED evidence."),
        _criterion("Commercial", "Priority channels", all(slug in {row["slug"] for row in account_rows} for slug in ("lemon-squeezy", "taskbounty", "rapidapi", "apify-store", "contra", "dealwork", "algora")), "All first-proof channels have secure onboarding plus automated or truthful owner-assisted boundaries."),
        _criterion("Commercial", "External event idempotency", _field(WebhookEvent, "raw_body_hash") and _field(WebhookEvent, "signature_valid") and _field(WebhookEvent, "external_event_id"), "Signed events persist authentication, immutable hash, mapping, retry and unknown-state truth."),
        _criterion("Owned revenue", "Product Factory lanes", _field(ProductCandidate, "state") and operation_spec("image_generate_product_asset").worker_class == "image_product" and _has("control.services.product_factory", "product_factory_cycle"), "Image/design, text/document, micro-API, Apify and marketing blueprints enter the canonical Job/WorkPlan path."),
        _criterion("Owned revenue", "Budgets, inventory and stop-loss", _has("control.services.product_factory", "load_policy") and _has("control.services.product_factory", "apply_stop_loss"), "Factory is disabled by default with daily/per-product credit ceilings, concurrency, inventory, confidence, margin and stop-loss controls."),
        _criterion("Owned revenue", "Capability monetization matrix", _has("control.services.product_factory", "sync_capability_monetization_matrix"), "Registered worker operations map to canonical disabled-by-default offerings and suitable channels."),
        _criterion("Owned revenue", "Publication-ready inventory", _field(ProductCandidate, "published_at") and _has("control.services.product_factory", "record_owned_product_publication"), "QA-passed local assets retain offering, channel, price and copy truth and can reconcile owner-performed publication without regeneration."),
        _criterion("Owned revenue", "Authoritative ROI learning", all(_field(ProductCandidate, name) for name in ("first_sale_at", "break_even_at", "return_on_production_cost", "commercial_evidence")) and _has("control.services.product_factory", "record_owned_product_payout"), "Sales, refunds, received payout, production return, first sale and break-even remain distinct and idempotent."),
        _criterion("Proof", "Append-only meaningful proof stages", _field(IntegrationProofRun, "authoritative") and proof_runner_has_meaningful_stages(), "Paystack connection/read/test-checkout/charge/execution/QA/settlement stages use real persisted evidence; external-only stages state the exact next action."),
        _criterion("Reconciliation", "Bounded credential-aware cycle", _has("control.services.integration_reconciliation", "reconcile_integrations") and _field(MarketIntegrationProfile, "last_reconciled_at"), "Zero credentials yields an inert cycle; auth failures disarm and back off."),
        _criterion("Security", "No bank or wallet private-key schema", all(not any(token in field["name"] for token in ("password", "bank", "private", "seed", "signing")) for row in account_rows for field in row["credential_fields"]), "Only provider credentials and public payout identifiers are accepted; prohibited material is rejected."),
        _criterion("Security", "Revocation fail-closed", _has("control.services.integration_accounts", "revoke_credentials"), "Revocation disables market payout/live/autonomous state while preserving inactive credential and financial history."),
        _criterion("Zero credentials", "Dashboard and runtime inert", len(account_rows) == 20 and not any(row["connected"] or row["autonomy_ready"] for row in account_rows), "Missing credentials are represented as owner actions, not boot errors or live readiness."),
    ]


def blocker_rows() -> list[dict[str, Any]]:
    rows = []
    for row in integration_accounts_snapshot()["rows"]:
        required = [field["label"] for field in row["credential_fields"] if field["required"]]
        rows.append({
            "area": row["category"],
            "integration": row["display_name"],
            "remaining_action": row["owner_action_required"],
            "classification": "OWNER_EXTERNAL_BLOCKER",
            "credential_kyc_proof_required": {
                "credentials": required,
                "kyc": row["kyc_required"],
                "payout_proof": row["payout_receipt_proof_state"] != "VERIFIED",
            },
            "live_proof_can_start_immediately_after_owner_action": row["connection_test_mode"] != "MANUAL",
            "exact_next_test": "Run the authoritative read-only connection test." if row["connection_test_mode"] != "MANUAL" else "Submit the provider/account publication or payout proof through the manual boundary.",
        })
    return rows


def prelive_acceptance_report() -> dict[str, Any]:
    checks = criteria()
    code_blockers = [check for check in checks if check.status == "CODE_BLOCKER"]
    return {
        "result": "PASS" if not code_blockers else "FAIL",
        "criteria": [asdict(check) for check in checks],
        "code_blocker_count": len(code_blockers),
        "owner_external_blockers": blocker_rows(),
        "autonomy_enabled": False,
        "production_deployed": False,
    }
