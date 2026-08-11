from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

import redis
from django.conf import settings
from django.db import DatabaseError, connection
from django.utils import timezone

from control.models import Job, OwnerSecurityProfile, Payout
from control.ops import SECTIONS
from control.services.admission import collect_metrics, decide_admission
from control.services.workload_policy import evaluate_text
from workers.registry import all_specs


VALID_STATUSES = {"PASS", "FAIL", "BLOCKED", "EXTERNAL_PROOF_REQUIRED"}


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    title: str
    status: str
    proof_scope: str
    evidence: str
    operator_action: str = ""

    def payload(self) -> dict[str, str]:
        return asdict(self)


def _criterion(identifier: str, title: str, status: str, scope: str, evidence: str, action: str = "") -> AcceptanceCriterion:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid acceptance status: {status}")
    return AcceptanceCriterion(identifier, title, status, scope, evidence, action)


def _ci_gate(identifier: str, title: str, evidence: str, *, ci_proven: bool) -> AcceptanceCriterion:
    if ci_proven:
        return _criterion(identifier, title, "PASS", "CI", evidence)
    return _criterion(
        identifier, title, "BLOCKED", "CI", "The implementation is present, but this invocation is not attached to the completed CI proof chain.",
        "Run the complete GitHub Actions workflow and invoke v1_acceptance --ci-proven only as its final sequential gate.",
    )


def _contract_gate(identifier: str, title: str, evidence: str, *, ci_proven: bool, markers: tuple[tuple[str, str], ...]) -> AcceptanceCriterion:
    missing = []
    for relative, marker in markers:
        try:
            present = marker in _source_text(relative)
        except OSError:
            present = False
        if not present:
            missing.append(f"{relative}:{marker}")
    if missing:
        return _criterion(
            identifier, title, "FAIL", "SOURCE", "Expanded V1 contract markers are missing: " + ", ".join(missing),
            "Restore the missing implementation and its deterministic/integration proof before freezing V1.",
        )
    return _ci_gate(identifier, title, evidence, ci_proven=ci_proven)


def _source_text(relative: str) -> str:
    return (Path(settings.BASE_DIR) / relative).read_text(encoding="utf-8")


def _safe_defaults() -> AcceptanceCriterion:
    expected = {
        "AMARKTAI_ENV": "development",
        "AUTONOMOUS_MODE": "OFF",
        "AGENTGIGS_AUTO_APPLY_ENABLED": "0",
        "SANDBOX_CODING_ENABLED": "0",
        "AGENTGIGS_MAX_GENX_CREDITS": "0",
        "DEPENDENCY_PREPARATION_ENABLED": "0",
        "DEALWORK_AUTO_ACQUIRE_ENABLED": "0",
        "CALLBOARD_AUTO_ACQUIRE_ENABLED": "0",
        "TASKBOUNTY_AUTO_ACQUIRE_ENABLED": "0",
        "OPIRE_AUTO_ACQUIRE_ENABLED": "0",
        "ALGORA_AUTO_ACQUIRE_ENABLED": "0",
        "SYNTHETIC_SPECULATIVE_INVENTORY_ENABLED": "0",
        "SAFETY_BOUNTY_EXECUTION_ENABLED": "0",
        "REPUTATION_INVESTMENT_ENABLED": "0",
        "REPUTATION_INVESTMENT_DAILY_LIMIT": "0",
        "NEVERMINED_AUTO_PUBLISH_ENABLED": "0",
        "SKYFIRE_AUTO_PUBLISH_ENABLED": "0",
        "HYRVE_AUTO_ACQUIRE_ENABLED": "0",
        "HYRVE_AUTO_PUBLISH_ENABLED": "0",
        "SERVICE_AUTO_PUBLISH_ENABLED": "0",
        "INBOUND_SERVICE_AUTO_ACCEPT_ENABLED": "0",
    }
    values = {}
    for raw in _source_text(".env.example").splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()
    mismatches = [f"{key}={values.get(key)!r}" for key, value in expected.items() if values.get(key) != value]
    if mismatches:
        return _criterion("safe_defaults", "Conservative production defaults", "FAIL", "SOURCE", "Unsafe or missing defaults: " + ", ".join(mismatches), "Restore fail-closed defaults before deployment.")
    return _criterion("safe_defaults", "Conservative production defaults", "PASS", "SOURCE", "Autonomy, market acquisition, coding, dependency preparation, GenX spend, speculative synthetic inventory, safety execution, and reputation-loss budget are disabled by default.")


def _money_truth() -> AcceptanceCriterion:
    expected_lifecycle = [
        Job.State.DISCOVERED, Job.State.EXPECTED, Job.State.CLAIMED, Job.State.AWARDED,
        Job.State.EXECUTING, Job.State.SUBMITTED, Job.State.ACCEPTED, Job.State.PAYOUT_PENDING, Job.State.SETTLED,
        Job.State.FAILED,
    ]
    payout_states = set(Payout.State.values)
    if list(Job.State.values) != expected_lifecycle or payout_states != {"EARNED", "PAYOUT_PENDING", "SETTLED", "REVERSED"}:
        return _criterion("money_truth", "Canonical money-state truth", "FAIL", "SOURCE", "Canonical job or payout states have drifted.", "Restore the reviewed economic state machine.")
    return _criterion("money_truth", "Canonical money-state truth", "PASS", "SOURCE", "SETTLED remains the only received-cash terminal state; expected, accepted, and pending values remain separate.")


def _owner_state() -> AcceptanceCriterion:
    try:
        profiles = OwnerSecurityProfile.objects.filter(
            user__is_active=True,
            user__is_staff=True,
            totp_confirmed_at__isnull=False,
        ).select_related("user")
        enrolled = any(
            profile.user.has_usable_password() and bool(profile.totp_secret_encrypted.strip())
            for profile in profiles
        )
    except DatabaseError:
        return _criterion("owner_login", "Owner login and MFA enrollment", "BLOCKED", "RUNTIME_CONFIG", "The owner security tables are unavailable.", "Apply migrations in the target environment, then bootstrap the owner.")
    if enrolled:
        return _criterion("owner_login", "Owner login and MFA enrollment", "PASS", "RUNTIME_CONFIG", "An active staff owner with a usable password and configured, confirmed TOTP is present.")
    return _criterion("owner_login", "Owner login and MFA enrollment", "BLOCKED", "RUNTIME_CONFIG", "No active staff owner with a usable password and configured, confirmed TOTP is present in this database.", "Run bootstrap_owner interactively in the target environment and retain recovery codes securely.")


def _postgres_redis(*, ci_proven: bool) -> AcceptanceCriterion:
    postgres_ok = False
    redis_ok = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            postgres_ok = connection.vendor == "postgresql" and cursor.fetchone() == (1,)
    except Exception:
        postgres_ok = False
    try:
        client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_connect_timeout=3, socket_timeout=3)
        redis_ok = bool(client.ping())
    except Exception:
        redis_ok = False
    if not postgres_ok or not redis_ok:
        missing = ", ".join(name for name, ok in (("PostgreSQL", postgres_ok), ("Redis", redis_ok)) if not ok)
        return _criterion("postgres_redis", "PostgreSQL and Redis state services", "BLOCKED", "RUNTIME", f"Unavailable or incorrect backend: {missing}.", "Run against the target PostgreSQL and Redis services, then repeat the acceptance command.")
    return _ci_gate("postgres_redis", "PostgreSQL and Redis state services", "PostgreSQL and Redis are reachable and the completed CI chain proved migrations, health, locking, and durable state behavior.", ci_proven=ci_proven)


def _resource_governor(*, ci_proven: bool) -> AcceptanceCriterion:
    try:
        metrics = collect_metrics()
        decision = decide_admission(purpose="ACCEPTANCE", metrics=metrics, persist=False)
    except DatabaseError:
        return _criterion("resource_governor", "Storage and resource admission", "BLOCKED", "RUNTIME", "Resource admission tables are unavailable.", "Apply migrations and repeat the governor probe.")
    except Exception as exc:
        return _criterion("resource_governor", "Storage and resource admission", "FAIL", "RUNTIME", f"Governor probe raised {exc.__class__.__name__}.", "Repair the governor probe before enabling work.")
    if not decision.allowed:
        return _criterion("resource_governor", "Storage and resource admission", "BLOCKED", "RUNTIME", "Current resource blockers: " + ", ".join(decision.reason_codes), "Free capacity or adjust reviewed VPS thresholds; do not bypass admission.")
    return _ci_gate("resource_governor", "Storage and resource admission", "Current resource probe is green and CI proved quota, memory, load, queue, and recovery decisions.", ci_proven=ci_proven)


def _worker_registry(*, ci_proven: bool) -> AcceptanceCriterion:
    expected = {
        "structured_data", "documents", "research", "localization", "transcription", "code_small", "code_heavy", "ci_testing", "media",
        "advanced_structured_data", "spreadsheet_reporting", "data_analysis", "technical_documentation", "content_copy", "seo_audit",
        "presentations", "document_production", "public_web_data", "web_output", "defensive_code_review", "customer_support",
        "synthetic_data", "ai_safety_research",
        "image_product",
    }
    specs = all_specs()
    actual = {spec.worker_class for spec in specs}
    invalid = sorted(spec.worker_class for spec in specs if not spec.operations or not spec.qa_profile or not spec.factory)
    if actual != expected or invalid:
        return _criterion("worker_execution", "Registered worker execution paths", "FAIL", "SOURCE", f"Registry mismatch: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}, invalid={invalid}.", "Restore complete registered V1 worker specifications.")
    return _ci_gate("worker_execution", "Registered worker execution paths", "All expanded V1 worker classes have production factories, operations, independent QA profiles, and completed deterministic/integration/container proofs.", ci_proven=ci_proven)


def _dashboard_contract(*, ci_proven: bool) -> AcceptanceCriterion:
    expected = {
        "overview", "live-work", "agents", "markets", "earnings", "treasury", "genx",
        "nodes", "storage", "performance", "logs", "alerts", "settings", "security",
    }
    if set(SECTIONS) != expected:
        return _criterion("dashboard", "Database-backed operations dashboard", "FAIL", "SOURCE", f"Dashboard section mismatch: missing={sorted(expected - set(SECTIONS))}, extra={sorted(set(SECTIONS) - expected)}.", "Restore every required database-backed operations section.")
    return _ci_gate("dashboard", "Database-backed operations dashboard", "CI proved all operations sections render persisted worker, market, execution, QA, resource, security, GenX, and money truth without secrets.", ci_proven=ci_proven)


def _sandbox_contract(*, ci_proven: bool) -> AcceptanceCriterion:
    broker = _source_text("sandbox_broker/server.py")
    workflow = _source_text(".github/workflows/ci.yml")
    required = ("--read-only", '"--cap-drop", "ALL"', "no-new-privileges:true", "--pids-limit", "--memory", "--cpus", "dependency-prep-smoke.py")
    missing = [marker for marker in required if marker not in broker and marker not in workflow]
    if missing or "/var/run/docker.sock:/var/run/docker.sock" in _source_text("sandbox/Dockerfile"):
        return _criterion("sandbox_isolation", "Coding sandbox isolation and secret exclusion", "FAIL", "SOURCE", "Sandbox contract markers missing or an agent image references the Docker socket: " + ", ".join(missing), "Restore the trusted-broker isolation boundary.")
    return _ci_gate("sandbox_isolation", "Coding sandbox isolation and secret exclusion", "The completed container gate built Aider/OpenHands/broker images, proved non-root bounded agents without a socket or controller secrets, and exercised dependency caches.", ci_proven=ci_proven)


def _prohibited_workloads() -> AcceptanceCriterion:
    samples = {
        "PROHIBITED_CRYPTO_MINING": "run a crypto mining pool",
        "PROHIBITED_DEPIN": "operate a depin node",
        "PROHIBITED_BANDWIDTH_RESALE": "sell unused bandwidth",
        "PROHIBITED_UNAUTHORIZED_SCANNING": "scan random networks",
        "PROHIBITED_SPAM": "launch a spam campaign",
        "PROHIBITED_FAKE_IDENTITY": "create a fake identity",
        "PROHIBITED_FRAUD": "enable fraud with stolen cards",
        "PROHIBITED_LOCAL_INFERENCE": "run an llm locally",
        "PROHIBITED_BROWSER_AUTOMATION": "uncontrolled browser automation",
    }
    failures = [code for code, sample in samples.items() if code not in evaluate_text(sample).reason_codes]
    if failures or not evaluate_text("resize an owned product image to 1200x628").allowed:
        return _criterion("prohibited_workloads", "Prohibited workload enforcement", "FAIL", "SOURCE", "Policy regression for: " + ", ".join(failures or ["benign workload"]), "Restore centralized fail-closed workload policy coverage.")
    return _criterion("prohibited_workloads", "Prohibited workload enforcement", "PASS", "SOURCE", "All nine prohibited infrastructure/work categories are blocked while a benign bounded media task remains allowed.")


def _readme_truth() -> AcceptanceCriterion:
    readme = _source_text("README.md")
    markers = ("CODE-COMPLETE", "CI-PROVEN", "disabled by default", "EXTERNAL_PROOF_REQUIRED", "Only `SETTLED`")
    missing = [marker for marker in markers if marker not in readme]
    if missing:
        return _criterion("readme_truth", "README implementation truth", "FAIL", "SOURCE", "README is missing truth markers: " + ", ".join(missing), "Update README without claiming external production or payout proof.")
    return _criterion("readme_truth", "README implementation truth", "PASS", "SOURCE", "README distinguishes implementation, CI proof, safe defaults, and external proof without claiming live revenue.")


def _two_sided_revenue_contracts(*, ci_proven: bool) -> list[AcceptanceCriterion]:
    definitions = (
        ("two_sided_revenue_engine", "Two-sided revenue engine", "Demand-pull and supply-push use centralized market, economic, lifecycle, and upgrade-safe catalog truth.", (("markets/revenue_catalog.py", "LEGACY_PROFILE_ENRICHMENTS"), ("tests/test_two_sided_revenue_integration.py", "test_existing_six_profiles_receive_static_taxonomy_without_dynamic_truth_reset"))),
        ("service_offering_truth", "Persisted service offering and listing truth", "CI proved SOURCE_PROVEN, EXECUTION_PROVEN, and runtime-gated SELLABLE progression plus fail-closed listings.", (("control/services/seller_services.py", "runtime_sellability_blockers"), ("tests/test_two_sided_revenue_integration.py", "test_proof_progression_and_runtime_sellability_gates_owner_count"))),
        ("inbound_order_uses_canonical_job_lifecycle", "Inbound orders use canonical jobs", "CI proved one canonical Job per authenticated idempotent inbound order.", (("control/models.py", "class InboundOrder"), ("control/services/seller_services.py", "receive_inbound_order"))),
        ("global_portfolio_ranking", "Global cross-source portfolio ranking", "CI ranked all channels while selecting only current eligible, action-allowed work within shared capacity.", (("control/services/revenue_portfolio.py", "would_select_if_enabled"), ("tests/test_two_sided_revenue_integration.py", "test_portfolio_ranks_blocked_work_but_selects_only_currently_safe_actions"))),
        ("seller_pricing_profit_floor", "Seller pricing preserves the profit floor", "CI proved bounded busy/idle price movement never undercuts the economic floor.", (("control/services/seller_services.py", "recommend_offering_price"), ("tests/test_two_sided_revenue_integration.py", "test_pricing_is_bounded_by_profit_floor"))),
        ("crypto_markets_offhost_only", "Crypto candidates are off-host only", "CI proved Webdock cannot initialize an off-host settlement candidate.", (("control/services/seller_protocols.py", "DisabledExternalSettlementBridge"), ("markets/revenue_catalog.py", "OFFHOST_SETTLEMENT_REQUIRED"))),
        ("nevermined_fiat_only_on_webdock", "Nevermined is fiat-only on Webdock", "CI rejected crypto/on-chain Nevermined plans and performed no mutation.", (("control/services/seller_protocols.py", "NEVERMINED_WEBDOCK_FIAT_ONLY"), ("tests/test_two_sided_revenue_stdlib.py", "test_nevermined_webdock_rejects_crypto"))),
        ("skyfire_noncrypto_gate", "Skyfire non-crypto settlement gate", "CI rejected unverified and COIN settlement paths.", (("control/services/seller_protocols.py", "SKYFIRE_NON_CRYPTO_SETTLEMENT_REQUIRED"), ("tests/test_two_sided_revenue_stdlib.py", "test_skyfire_rejects_coin_settlement"))),
        ("hyrve_fail_closed_without_contract", "HYRVE fails closed without a public contract", "CI proved source_wired remains false and no mutation adapter exists.", (("markets/revenue_catalog.py", "PUBLIC_API_CONTRACT_NOT_VERIFIED"), ("tests/test_two_sided_revenue_integration.py", "test_hyrve_has_no_invented_source_contract"))),
        ("service_capability_requires_execution_proof", "Service capability requires execution proof", "CI proved registry presence alone cannot produce a sellable service.", (("control/services/seller_services.py", "refresh_service_offering_proof"), ("tests/test_two_sided_revenue_integration.py", "test_service_requires_real_execution_and_qa_proof"))),
        ("coding_service_blocked_when_sandbox_off", "Coding services follow sandbox safety", "CI proved coding proof stops at EXECUTION_PROVEN while sandbox coding is off and owner sellable counts remain truthful.", (("control/services/seller_services.py", "CODING_SERVICE_BLOCKED_SANDBOX_OFF"), ("tests/test_two_sided_revenue_integration.py", "test_proof_progression_and_runtime_sellability_gates_owner_count"))),
        ("public_web_service_blocked_when_web_disabled", "Public-web services follow direct-web safety", "CI proved direct public-web proof stops at EXECUTION_PROVEN while web data is off.", (("control/services/seller_services.py", "PUBLIC_WEB_SERVICE_BLOCKED_WEB_DISABLED"), ("tests/test_two_sided_revenue_integration.py", "test_proof_progression_and_runtime_sellability_gates_owner_count"))),
        ("market_policy_staleness_blocks_mutation", "Stale seller policy blocks mutation", "CI proved seller-side mutations fail closed after policy evidence expires.", (("control/services/seller_services.py", "MARKET_POLICY_STALE"), ("tests/test_two_sided_revenue_integration.py", "test_stale_policy_blocks_listing"))),
        ("only_settled_is_cash", "Only authoritative settled evidence is cash", "CI proved canonical finance, ledger, treasury, idempotency, amount-mutation, and reversal truth for inbound settlement.", (("control/services/seller_services.py", "record_payout_state"), ("tests/test_two_sided_revenue_integration.py", "test_authoritative_settlement_uses_canonical_finance_and_reversal_truth"))),
    )
    return [
        _contract_gate(identifier, title, evidence, ci_proven=ci_proven, markers=markers)
        for identifier, title, evidence, markers in definitions
    ]


def build_acceptance_report(*, ci_proven: bool = False) -> dict:
    criteria = [
        _safe_defaults(),
        _money_truth(),
        _owner_state(),
        _ci_gate("jwt_replay", "JWT refresh rotation and replay-family revocation", "CI proved access/refresh rotation, replay detection, and family revocation.", ci_proven=ci_proven),
        _ci_gate("totp_recovery", "TOTP and one-time recovery", "CI proved password pre-authentication, TOTP, invalid-code rejection, and one-time recovery-code consumption.", ci_proven=ci_proven),
        _postgres_redis(ci_proven=ci_proven),
        _resource_governor(ci_proven=ci_proven),
        _ci_gate("acquisition_gates", "Autonomy, capability preflight, economics, and global locks", "CI proved OFF/SHADOW/LOW_RISK/FULL semantics, separate market opt-in, exact registry preflight, profitability gates, and fencing locks.", ci_proven=ci_proven),
        _contract_gate(
            "growth_governor", "Persisted Growth Governor, targets, and stages",
            "CI proved migration-backed targets/evaluations, AHEAD/ON_TRACK/BEHIND/INSUFFICIENT_DATA evaluation, and BOOTSTRAP/ESTABLISH/PROFIT/SCALE classification without converting targets into revenue claims.",
            ci_proven=ci_proven, markers=(("control/services/profit_brain.py", "evaluate_growth_targets"), ("control/models.py", "class GrowthEvaluation"), ("tests/test_profit_brain_integration.py", "test_growth")),
        ),
        _contract_gate(
            "uncapped_profit_governor", "Uncapped earning with economically bounded downside",
            "CI proved targets and BOOTSTRAP are not earnings caps, paid-cost approval scales with profitable job value, losses and unavailable paid resources remain blocked, concurrency remains fail-closed, and net settled profit uses only settled cash and attributable persisted actual cost.",
            ci_proven=ci_proven,
            markers=(
                ("control/acquisition.py", "paid_cost_envelope"),
                ("control/services/profit_brain.py", "OBJECTIVE_FLOOR_NEVER_EARNINGS_CAP"),
                ("control/services/profit_brain.py", "settled_profit_truth"),
                ("tests/test_profit_brain_integration.py", "test_bootstrap_and_exceeded_targets_never_cap_profitable_work"),
            ),
        ),
        *_two_sided_revenue_contracts(ci_proven=ci_proven),
        _contract_gate(
            "genx_async_session_truth", "GenX asynchronous session and billing truth",
            "CI proved session acknowledgements remain SUBMITTED until their remote job is terminal, remote identities are persisted separately, missing usage remains unresolved and reserved, assistant-only output is selected safely, reconciliation is idempotent, and incomplete monetary coverage is never presented as final true profit.",
            ci_proven=ci_proven,
            markers=(
                ("gateways/genx/service.py", "reconcile_remote_job_payload"),
                ("gateways/genx/output.py", "extract_session_assistant_text"),
                ("tests/test_genx_async_session_integration.py", "test_completed_without_usage_keeps_reservation"),
                ("control/services/profit_brain.py", "ATTRIBUTABLE_GENX_MONETARY_COST_COVERAGE_INCOMPLETE"),
            ),
        ),
        _contract_gate(
            "utilization_economics", "Utilization-aware economics and no-avoidable-idle invariant",
            "CI proved capacity persistence, idle micro-profit acceptance, busy opportunity-cost preference, resource reservation, and avoidable-idle accounting.",
            ci_proven=ci_proven, markers=(("control/services/profit_brain.py", "capture_capacity"), ("control/services/profit_brain.py", "BETTER_COMMITTED_WORK_HAS_PRIORITY"), ("tests/test_profit_brain_integration.py", "avoidable_idle")),
        ),
        _contract_gate(
            "adaptive_economic_learning", "Bounded pricing, exploration, reputation, and learning",
            "CI proved persisted adaptive pricing, bounded exploration allocation, conservative reputation signals, rolling performance aggregates, and disabled-by-default reputation-loss investment.",
            ci_proven=ci_proven, markers=(("control/services/profit_brain.py", "recommend_price"), ("control/services/profit_brain.py", "record_reputation_snapshot"), ("control/models.py", "class StrategyAdjustment")),
        ),
        _contract_gate(
            "multifile_composite", "Safe multi-file ingestion and composite DAG execution",
            "CI proved file-count/size/path/type limits, hashes and deduplication, immutable manifests, dependency ordering, step-specific worker execution, QA gates, and bounded repair.",
            ci_proven=ci_proven, markers=(("planning/services.py", "rebuild_asset_manifest"), ("planning/services.py", "_build_composite_plan"), ("tests/test_multifile_composite_integration.py", "test_explicit_dag")),
        ),
        _worker_registry(ci_proven=ci_proven),
        _contract_gate(
            "expanded_worker_qa", "Expanded worker catalogue and independent QA",
            "CI executed the expanded deterministic worker catalogue and reopened deliverables through operation-specific QA profiles.",
            ci_proven=ci_proven, markers=(("workers/registry.py", "advanced_structured_data"), ("workers/qa/runtime.py", "defensive_review"), ("tests/test_expanded_workers_integration.py", "Expanded")),
        ),
        _contract_gate(
            "synthetic_data_factory", "Commissioned-first Synthetic Data Factory",
            "CI proved schema-driven generation, validation, privacy/provenance checks, deduplication, deterministic splits, dataset cards, persisted economics, and reopen QA; speculative inventory remains disabled by default.",
            ci_proven=ci_proven, markers=(("workers/synthetic_data/worker.py", "SYNTHETIC_INVENTORY_NOT_EXPLICITLY_AUTHORIZED"), ("control/services/synthetic_data.py", "persist_synthetic_dataset_run"), ("tests/test_synthetic_safety_integration.py", "test_commissioned_synthetic")),
        ),
        _contract_gate(
            "authorized_safety_research", "Authorized bounded AI-safety research lane",
            "CI proved NO SCOPE = NO TESTING, exact-current-target and immediate reauthorization gates, local-fixture-only bounded execution, prohibited-technique rejection, independent reproduction, duplicate checks, and draft-only submission truth.",
            ci_proven=ci_proven, markers=(("control/services/safety_research.py", "NO_SCOPE_NO_TESTING"), ("control/services/safety_research.py", "SAFETY_ATTEMPT_REAUTHORIZATION_FAILED"), ("tests/test_synthetic_safety_integration.py", "test_authorization_is_rechecked")),
        ),
        _contract_gate(
            "multi_market_adapters", "Multi-market adapter contracts and payout fail-closed truth",
            "CI proved AgentGigs plus Dealwork, Callboard, TaskBounty, Opire, and Algora source/adapter contracts with mocked externals, explicit policy/payout blockers, disabled acquisition, and prohibited crypto payout-route rejection.",
            ci_proven=ci_proven, markers=(("control/services/markets.py", "DealworkAdapter"), ("control/services/markets.py", "AlgoraAdapter"), ("tests/test_multi_market_adapters_integration.py", "payout")),
        ),
        _ci_gate("qa_repair", "Independent QA and bounded repair", "CI proved independent QA records, bounded repair attempts, and fail-closed submission gating.", ci_proven=ci_proven),
        _ci_gate("lifecycle_logic", "Submission, revision, payout, and ledger lifecycle logic", "CI proved idempotent lifecycle transitions and that only reconciled SETTLED payouts become received cash.", ci_proven=ci_proven),
        _dashboard_contract(ci_proven=ci_proven),
        _contract_gate(
            "dashboard_economic_truth", "Expanded dashboard economic, utilization, market, and alert truth",
            "CI proved settled-only cash cards, expected/exposure labels, persisted growth/utilization/profitability data, complete worker and market truth, derived safety/synthetic/resource alerts, and secret redaction.",
            ci_proven=ci_proven, markers=(("control/ops.py", "BLOCKED PROFITABLE OPPORTUNITIES 24H"), ("control/ops.py", "SAFETY_SCOPE_EXPIRY"), ("tests/test_ops_dashboard_integration.py", "sensitive")),
        ),
        _ci_gate("watchdog_recovery", "Watchdog, reconciliation, and retention", "CI proved stale-state recovery, safe retry classification, unknown-remote reconciliation, cleanup exclusions, and audit records.", ci_proven=ci_proven),
        _sandbox_contract(ci_proven=ci_proven),
        _ci_gate("media_runtime", "Bounded deterministic media runtime", "CI built the FFmpeg/Pillow production image and proved image/audio output plus independent QA and output bounds.", ci_proven=ci_proven),
        _prohibited_workloads(),
        _readme_truth(),
        _criterion("public_https", "Public HTTPS at earn.amarktai.co.za", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "Repository and Caddy configuration cannot prove public DNS/TLS reachability.", "Deploy to Webdock, verify DNS, TLS certificate, security headers, and owner-only access from outside the VPS."),
        _criterion("actual_reboot", "Actual Webdock reboot recovery", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "CI proves restart configuration and reconciliation logic, not a physical VPS reboot.", "Perform a controlled Webdock reboot and retain service, heartbeat, queue, and reconciliation evidence."),
        _criterion("live_genx", "Live GenX credentials and budgeted call", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "No live credential or billable call is exercised by CI.", "Configure the production GenX key, sync the live catalog, execute one capped call, and reconcile actual credits."),
        _criterion("live_market_account", "Live market authentication, policy, KYC, and payout readiness", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "AgentGigs, Dealwork, Callboard, TaskBounty, Opire, and Algora integrations use deterministic CI fixtures; live availability, policies, account access, and South African payout eligibility require external evidence.", "For each market retained for production, verify the current official integration surface and automation policy, authenticate the real account where supported, complete KYC, and persist South African payout evidence without exposing credentials."),
        _criterion("live_opportunity", "Real opportunity discovery and acquisition", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "CI uses deterministic fixtures and never performs a live irreversible marketplace mutation.", "Begin in SHADOW, validate real discovered opportunities, then explicitly enable LOW_RISK and the separate market switch for a funded eligible job."),
        _criterion("settled_cash", "Real execution, remote submission, approval, payout, and SETTLED cash", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "Code and CI cannot manufacture a funded job or received cash.", "Complete one real funded job through remote submission, approval, payout reconciliation, and bank/rail-confirmed SETTLED posting."),
    ]
    counts = {status: sum(1 for row in criteria if row.status == status) for status in sorted(VALID_STATUSES)}
    overall = "FAIL" if counts["FAIL"] else "BLOCKED" if counts["BLOCKED"] else "EXTERNAL_PROOF_REQUIRED" if counts["EXTERNAL_PROOF_REQUIRED"] else "PASS"
    return {
        "schema_version": 2,
        "generated_at": timezone.now().isoformat(),
        "ci_proven_context": ci_proven,
        "overall_status": overall,
        "counts": counts,
        "criteria": [row.payload() for row in criteria],
        "terminology": {
            "CODE-COMPLETE": "Implementation exists in the repository.",
            "CI-PROVEN": "The required deterministic/stateful/container gates completed in one sequential CI run.",
            "SOURCE-WIRED": "A real external adapter path exists but may lack live proof.",
            "RUNTIME-PROVEN": "The target production environment exercised the path successfully.",
            "PAYOUT-PROVEN": "A real marketplace payout was reconciled.",
            "SETTLED": "Cash was actually received and reconciled.",
        },
    }
