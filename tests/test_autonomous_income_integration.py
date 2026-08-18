from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from control.models import Claim, Job, LedgerEntry, Marketplace, Payout
from control.services.autonomous_income import (
    ACTION_BY_KEY,
    AdmissionResult,
    EarnControls,
    SourceClassification,
    admit_opportunity,
    autonomous_earn_snapshot,
    claim_once,
    estimate_expected_settled_profit,
    no_external_mutation_cycle,
    portfolio_candidate,
)
from control.services.autonomous_income_acceptance import REMOVED_CRYPTO_MARKETS, autonomous_income_acceptance_report
from control.services.autonomy import AutonomyMode
from control.services.finance import record_payout_state
from control.services.payment_rails import DEFAULT_PAYMENT_RAILS
from control.services.revenue_portfolio import rank_portfolio_candidates
from control.services.workload_policy import evaluate_text
from markets.algora.client import AlgoraAdapter
from markets.base import MarketAdapter, MarketCapabilities, NormalizedOpportunity
from markets.catalog import BY_SLUG as WORK_MARKETS
from markets.gitpay.client import GitpayAdapter
from markets.opire.client import OpireAdapter
from markets.revenue_catalog import BY_SLUG as REVENUE_MARKETS
from markets.taskbounty.client import TaskBountyAdapter


ROOT = Path(__file__).resolve().parents[1]


class _Response:
    ok = True
    status_code = 200
    content = b"{}"
    headers = {}

    def __init__(self, payload):
        self.payload = payload
        self.content = __import__("json").dumps(payload).encode()

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _Response(self.payload)


class _ClaimAdapter(MarketAdapter):
    slug = "taskbounty"
    capabilities = MarketCapabilities(claim=True)

    def __init__(self):
        self.calls = 0

    def health(self):
        return {"ok": True}

    def payout_status(self):
        return {"ready": False}

    def discover_jobs(self):
        return []

    def normalize_job(self, raw):
        return NormalizedOpportunity(str(raw.get("id") or "task"), "Task", "bug", Decimal("100"), raw=raw)

    def claim(self, job):
        self.calls += 1
        return {"claim_id": "claim-1"}


class AutonomousIncomeIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(slug="taskbounty", display_name="TaskBounty")

    def opportunity(self, **overrides):
        values = {
            "external_id": "task-1", "title": "Fix bounded parser bug", "task_class": "bug",
            "reward": Decimal("100"), "action": "TASKBOUNTY_BUG_FIX", "fee_rate": Decimal("0.20"),
            "payout_probability": Decimal("0.90"), "acceptance_probability": Decimal("0.90"),
            "expected_provider_cost": Decimal("2"), "expected_execution_cost": Decimal("1"),
            "expected_minutes": 60, "source_classification": "MARKETPLACE_DISCOVERY", "raw": {},
        }
        values.update(overrides)
        return NormalizedOpportunity(**values)

    def permissive_controls(self):
        return EarnControls(max_active_claims=5, max_new_claims_per_hour=5, max_market_concentration=Decimal("1"), min_expected_net_profit=Decimal("0"), min_expected_profit_per_minute=Decimal("0"), min_feasibility_score=Decimal("0"), max_provider_cost_at_risk=Decimal("100"), max_unsettled_market_exposure=Decimal("1000"), max_repair_attempts=1)

    def test_01_tesseract_absent_from_production_image(self):
        self.assertNotIn("tesseract-ocr", (ROOT / "Dockerfile").read_text(encoding="utf-8").casefold())

    def test_02_ocr_cannot_invoke_local_neural_runtime(self):
        source = (ROOT / "workers" / "ocr" / "worker.py").read_text(encoding="utf-8").casefold()
        self.assertNotIn("_tesseract", source)
        self.assertIn("visionworker", source)

    def test_03_crypto_markets_absent(self):
        self.assertFalse((set(WORK_MARKETS) | set(REVENUE_MARKETS)) & REMOVED_CRYPTO_MARKETS)

    def test_04_wallet_payout_rejected(self):
        self.assertNotIn("crypto-wallet", DEFAULT_PAYMENT_RAILS)
        self.assertNotIn("valr", DEFAULT_PAYMENT_RAILS)

    def test_05_taskbounty_crypto_payout_method_rejected(self):
        adapter = TaskBountyAdapter("key", session=_Session({}))
        with self.assertRaises(ValueError):
            adapter.set_payout_method("solana_usdc", "address")

    def test_06_prohibited_webdock_task_rejected(self):
        self.assertFalse(evaluate_text("Run a Bitcoin mining node").allowed)

    def test_07_network_scan_task_rejected(self):
        self.assertFalse(evaluate_text("Perform an nmap network scan").allowed)

    def test_08_spam_task_rejected(self):
        self.assertFalse(evaluate_text("Send mass unsolicited messages").allowed)

    def test_09_bounded_legitimate_coding_task_allowed(self):
        self.assertTrue(evaluate_text("Fix the supplied parser and run its unit tests").allowed)

    def test_10_taskbounty_discovery_normalization(self):
        session = _Session({"tasks": [{"id": "tb-1", "title": "Coverage uplift", "reward": 50, "type": "coverage"}]})
        job = TaskBountyAdapter("key", session=session).normalize_job(TaskBountyAdapter("key", session=session).discover_jobs()[0])
        self.assertEqual(job.action, "TASKBOUNTY_COVERAGE")

    def test_11_taskbounty_bug_roi(self):
        estimate = estimate_expected_settled_profit(self.opportunity())
        self.assertGreater(estimate.expected_net, 0)

    def test_12_taskbounty_coverage_roi(self):
        estimate = estimate_expected_settled_profit(self.opportunity(action="TASKBOUNTY_COVERAGE"))
        self.assertGreater(estimate.expected_profit_per_minute, 0)

    def test_13_taskbounty_duplicate_claim_idempotency(self):
        job = Job.objects.create(marketplace=self.market, external_id="dup", title="Task", task_class="bug", reward=100, normalized_payload={"id": "dup"})
        admission = admit_opportunity(self.opportunity(), market="taskbounty", feasibility_score=Decimal("0.9"), controls=self.permissive_controls())
        adapter = _ClaimAdapter()
        with patch("control.services.autonomous_income.current_mode", return_value=AutonomyMode.LOW_RISK):
            first = claim_once(adapter=adapter, job_id=job.id, admission=admission)
            second = claim_once(adapter=adapter, job_id=job.id, admission=admission)
        self.assertTrue(first["performed"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(adapter.calls, 1)

    def test_14_failing_tests_prevent_submission(self):
        source = (ROOT / "control" / "services" / "submission.py").read_text(encoding="utf-8")
        self.assertIn('status="QA_PASSED"', source)
        self.assertIn("require_submission_ready", source)

    def test_15_opire_try_lifecycle(self):
        self.assertEqual(OpireAdapter.github_try_command(), "/try")

    def test_16_opire_claim_lifecycle(self):
        self.assertEqual(OpireAdapter.github_claim_command(42), "/claim #42")

    def test_17_algora_bounty_normalization(self):
        job = AlgoraAdapter(lambda **_: []).normalize_job({"id": "a1", "task": {"title": "Fix"}, "reward": 25})
        self.assertEqual(job.action, "ALGORA_BOUNTY")

    def test_18_algora_duplicate_claim_safety(self):
        source = (ROOT / "control" / "services" / "autonomous_income.py").read_text(encoding="utf-8")
        self.assertIn("existing = Claim.objects.filter(job=job)", source)

    def test_19_gitpay_waits_for_assignment(self):
        opportunity = GitpayAdapter(lambda **_: []).normalize_job({"id": "g1", "title": "Fix", "reward": 20})
        self.assertIn("WAITING_FOR_EXPLICIT_ASSIGNMENT", GitpayAdapter().eligibility(opportunity)["reason_codes"])

    def test_20_gitpay_no_provider_spend_before_assignment(self):
        opportunity = GitpayAdapter().normalize_job({"id": "g1", "title": "Fix", "reward": 20})
        self.assertFalse(GitpayAdapter().eligibility(opportunity)["provider_spend_allowed"])

    def test_21_funded_feature_workflow(self):
        self.assertIn("tests", ACTION_BY_KEY["FUNDED_FEATURE_WORK"].execution_workflow)

    def test_22_funded_tests_docs_workflow(self):
        self.assertIn("behavioral_proof", ACTION_BY_KEY["FUNDED_TEST_DOCS_REFACTOR"].execution_workflow)

    def test_23_low_value_opportunity_rejected(self):
        result = admit_opportunity(self.opportunity(reward=Decimal("1")), market="taskbounty", feasibility_score=Decimal("0.9"))
        self.assertFalse(result.allowed)

    def test_24_crowded_opportunity_penalized(self):
        open_estimate = estimate_expected_settled_profit(self.opportunity())
        crowded = estimate_expected_settled_profit(self.opportunity(competition={"solvers": 10, "existing_prs": 3}))
        self.assertLess(crowded.expected_net, open_estimate.expected_net)

    def test_25_high_payout_risk_penalized(self):
        safe = estimate_expected_settled_profit(self.opportunity())
        risky = estimate_expected_settled_profit(self.opportunity(payout_probability=Decimal("0.2")))
        self.assertLess(risky.expected_net, safe.expected_net)

    def test_26_profitable_small_task_outranks_risky_large_task(self):
        small = self.opportunity(external_id="small", reward=Decimal("35"), expected_minutes=30, payout_probability=Decimal("0.95"), acceptance_probability=Decimal("0.95"), fee_rate=Decimal("0"), expected_provider_cost=Decimal("0"), expected_execution_cost=Decimal("0"))
        large = self.opportunity(external_id="large", reward=Decimal("100"), expected_minutes=180, payout_probability=Decimal("0.2"), acceptance_probability=Decimal("0.2"), fee_rate=Decimal("0"), expected_provider_cost=Decimal("0"), expected_execution_cost=Decimal("0"))
        a = AdmissionResult(True, (), estimate_expected_settled_profit(small), Decimal("0.9"), True)
        b = AdmissionResult(True, (), estimate_expected_settled_profit(large), Decimal("0.9"), True)
        ranked = rank_portfolio_candidates([portfolio_candidate(small, a, job_id="small"), portfolio_candidate(large, b, job_id="large")], available_slots=2, productive_minutes_available=Decimal("500"))
        self.assertEqual(ranked[0].candidate.job_id, "small")

    def test_27_api_demand_competes_in_same_portfolio(self):
        row = portfolio_candidate(self.opportunity(action="MARKETPLACE_API_ACTOR_INCOME", source_classification="BUILT_IN_DEMAND"), AdmissionResult(True, (), estimate_expected_settled_profit(self.opportunity()), Decimal(".9"), True), job_id="api")
        self.assertEqual(row.revenue_channel, "PAY_PER_CALL_API")

    def test_28_apify_demand_competes_in_same_portfolio(self):
        self.assertIn("apify-store", ACTION_BY_KEY["MARKETPLACE_API_ACTOR_INCOME"].markets)

    def test_29_provider_spend_envelope_respected(self):
        result = admit_opportunity(self.opportunity(expected_provider_cost=Decimal("30")), market="taskbounty", feasibility_score=Decimal(".9"))
        self.assertIn("PROVIDER_COST_AT_RISK_EXCEEDED", result.reason_codes)

    def test_30_untrusted_repo_cannot_access_production_secrets(self):
        source = (ROOT / "sandbox_broker" / "server.py").read_text(encoding="utf-8")
        self.assertIn('"--network", "none"', source)
        self.assertNotIn("DJANGO_SECRET_KEY", source)

    def test_31_repo_execution_timeout(self):
        source = (ROOT / "sandbox_broker" / "server.py").read_text(encoding="utf-8")
        self.assertIn("SANDBOX_EXECUTION_TIMEOUT_SECONDS", source)
        self.assertIn("except subprocess.TimeoutExpired", source)

    def test_32_submission_reconciliation_idempotent(self):
        source = (ROOT / "control" / "services" / "submission.py").read_text(encoding="utf-8")
        self.assertIn("if job.state == Job.State.SUBMITTED", source)
        self.assertIn("UNKNOWN_REMOTE_STATE", source)

    def test_33_payout_reconciliation_idempotent(self):
        source = (ROOT / "control" / "services" / "finance.py").read_text(encoding="utf-8")
        self.assertIn("_post_once", source)
        self.assertIn("ledger idempotency conflict", source)

    def test_34_accepted_is_not_settled(self):
        job = Job.objects.create(marketplace=self.market, external_id="accepted", title="Task", task_class="bug", reward=10, state=Job.State.ACCEPTED)
        self.assertFalse(Payout.objects.filter(job=job, state=Payout.State.SETTLED).exists())

    def test_35_pending_payout_is_not_revenue(self):
        job = Job.objects.create(marketplace=self.market, external_id="pending", title="Task", task_class="bug", reward=10, state=Job.State.ACCEPTED)
        record_payout_state(job_id=job.id, target_state=Payout.State.EARNED, gross=Decimal("10"))
        record_payout_state(job_id=job.id, target_state=Payout.State.PAYOUT_PENDING, gross=Decimal("10"))
        self.assertEqual(LedgerEntry.objects.filter(event_type="PAYOUT_SETTLED").count(), 0)

    def test_36_settled_transaction_increments_profit_once(self):
        job = Job.objects.create(marketplace=self.market, external_id="settled", title="Task", task_class="bug", reward=10, state=Job.State.SUBMITTED)
        record_payout_state(job_id=job.id, target_state=Payout.State.EARNED, gross=Decimal("10"))
        record_payout_state(job_id=job.id, target_state=Payout.State.PAYOUT_PENDING, gross=Decimal("10"))
        record_payout_state(job_id=job.id, target_state=Payout.State.SETTLED, gross=Decimal("10"))
        record_payout_state(job_id=job.id, target_state=Payout.State.SETTLED, gross=Decimal("10"))
        self.assertEqual(LedgerEntry.objects.filter(event_type="PAYOUT_SETTLED").count(), 1)

    def test_37_marketing_dependent_source_excluded(self):
        result = admit_opportunity(self.opportunity(source_classification="MARKETING_DEPENDENT"), market="taskbounty", feasibility_score=Decimal(".9"), controls=self.permissive_controls())
        self.assertIn("MARKETING_DEPENDENT_EXCLUDED", result.reason_codes)

    def test_38_autonomous_mode_off_has_no_external_mutation(self):
        with patch.dict(os.environ, {"AUTONOMOUS_MODE": "OFF"}):
            result = no_external_mutation_cycle(adapters=[_ClaimAdapter()])
        self.assertEqual(result["external_mutations"], 0)
        self.assertEqual(result["adapter_calls"], 0)

    def test_39_phase1_gate_remains_present(self):
        self.assertTrue((ROOT / "control" / "management" / "commands" / "phase1_acceptance.py").is_file())

    def test_40_phase2_gate_remains_present(self):
        self.assertTrue((ROOT / "control" / "management" / "commands" / "phase2_acceptance.py").is_file())

    def test_41_phase3_gate_remains_present(self):
        self.assertTrue((ROOT / "control" / "management" / "commands" / "phase3_acceptance.py").is_file())

    def test_42_launch_gate_remains_present(self):
        self.assertTrue((ROOT / "control" / "management" / "commands" / "launch_acceptance.py").is_file())

    def test_43_autonomous_income_acceptance_green(self):
        with patch.dict(os.environ, {"AUTONOMOUS_MODE": "OFF"}):
            report = autonomous_income_acceptance_report(repository_root=ROOT)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["counts"]["partial"], 0)
        self.assertEqual(report["counts"]["unknown"], 0)

    def test_44_dashboard_zero_settlement_truth(self):
        snapshot = autonomous_earn_snapshot()
        cards = {row["label"]: row["value"] for row in snapshot["cards"]}
        self.assertEqual(cards["SETTLED TODAY"], "0")
        self.assertEqual(cards["TRUE NET PROFIT"], "0.00")

    def test_45_owner_console_exposes_autonomous_earn_view(self):
        sidebar = (ROOT / "control" / "templates" / "control" / "partials" / "sidebar.html").read_text(encoding="utf-8")
        script = (ROOT / "control" / "static" / "control" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/ops/autonomous-earn/", sidebar)
        self.assertIn('"autonomous-earn": ["autonomous-earn"]', script)
