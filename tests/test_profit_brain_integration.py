from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from control.models import (
    AcquisitionPreflight,
    Alert,
    CapacitySnapshot,
    Execution,
    GrowthEvaluation,
    GrowthTarget,
    Job,
    JobScore,
    MarketPolicyVersion,
    Marketplace,
    OpportunityDecision,
    Payout,
    PerformanceAggregate,
    PricingStrategy,
    QAResult,
    ReputationSnapshot,
    Worker,
)
from control.services.profit_brain import (
    GrowthStage,
    UtilizationState,
    capture_capacity,
    ensure_growth_targets,
    evaluate_growth_targets,
    evaluate_opportunity,
    record_reputation_snapshot,
    refresh_performance,
)
from control.services.agentgigs import score_open_jobs


class ProfitBrainIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(
            slug="profit-market", display_name="Profit Market", enabled=True,
            status=Marketplace.Status.LIVE, payout_ready=True, south_africa_verified=True,
        )

    def _job(self, external_id: str, *, profit="1.00", ppm="0.10", state=Job.State.EXPECTED, payload=None):
        job = Job.objects.create(
            marketplace=self.market, external_id=external_id, title="Profitable bounded task",
            task_class="Data Analysis", reward="2.00", state=state,
            normalized_payload=payload or {"operation": "json_to_csv", "source_filename": "input.json"},
        )
        JobScore.objects.create(
            job=job, p_acquire="0.8", p_accept="0.9", p_payment="0.95",
            expected_cash="1.50", expected_profit=profit, expected_profit_per_minute=ppm,
            expected_minutes=10, max_genx_credits="0",
        )
        return job

    def _capacity(self, state=UtilizationState.IDLE, available=4):
        return CapacitySnapshot(
            productive_slots=4, active_slots=4 - available, available_slots=available,
            reserved_slots=0, utilization="0" if available else "1",
            utilization_state=state.value,
        )

    def test_idle_capacity_accepts_micro_profit_without_fixed_dollar_floor(self):
        job = self._job("micro", profit="0.15", ppm="0.015")
        decision = evaluate_opportunity(job, capacity=self._capacity(), capability="structured_data")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.expected_cash_profit, Decimal("0.15"))
        self.assertNotIn("NON_POSITIVE_EXPECTED_PROFIT", decision.reason_codes)

    def test_busy_capacity_protects_better_committed_work(self):
        better = self._job("better", profit="20", ppm="2", state=Job.State.AWARDED)
        candidate = self._job("candidate", profit="2", ppm="0.20")
        decision = evaluate_opportunity(
            candidate,
            capacity=self._capacity(UtilizationState.BUSY, available=0),
            capability="structured_data",
        )
        self.assertFalse(decision.allowed)
        self.assertIn("BETTER_COMMITTED_WORK_HAS_PRIORITY", decision.reason_codes)
        self.assertGreater(decision.opportunity_cost, 0)

    def test_expected_loss_requires_explicit_bounded_reputation_budget(self):
        job = self._job(
            "reputation", profit="-0.50", ppm="-0.05",
            payload={"operation": "json_to_csv", "source_filename": "input.json", "estimated_reputation_value": "1"},
        )
        blocked = evaluate_opportunity(job, capacity=self._capacity(), capability="structured_data")
        self.assertFalse(blocked.allowed)
        self.assertIn("NON_POSITIVE_EXPECTED_PROFIT", blocked.reason_codes)
        with patch.dict(os.environ, {"REPUTATION_INVESTMENT_ENABLED": "1", "REPUTATION_INVESTMENT_DAILY_LIMIT": "1.00"}, clear=False):
            allowed = evaluate_opportunity(job, capacity=self._capacity(), capability="structured_data")
        self.assertTrue(allowed.allowed)
        self.assertTrue(allowed.reputation_investment)

    def test_capacity_persists_avoidable_idle_and_alert(self):
        job = self._job("waiting", profit="3", ppm="0.30")
        AcquisitionPreflight.objects.create(
            job=job, autonomy_mode="SHADOW", operation="json_to_csv", worker_class="structured_data",
            eligible=True, allowed=False, reason_codes=["AUTONOMY_SHADOW_ONLY"],
        )
        with patch.dict(os.environ, {"AMARKTAI_PRODUCTIVE_CAPACITY_SLOTS": "2"}, clear=False):
            snapshot = capture_capacity()
        self.assertEqual(snapshot.profitable_eligible_waiting, 1)
        self.assertGreater(snapshot.avoidable_idle_minutes, 0)
        self.assertGreater(snapshot.estimated_foregone_profit, 0)
        self.assertTrue(Alert.objects.filter(alert_type="AVOIDABLE_IDLE_PROFITABLE_WORK").exists())

    def test_growth_targets_and_explanations_are_persisted(self):
        self._job("growth-sample")
        targets = ensure_growth_targets()
        evaluation = evaluate_growth_targets()
        self.assertEqual(len(targets), 8)
        self.assertIn(evaluation.status, {"AHEAD", "ON_TRACK", "BEHIND", "INSUFFICIENT_DATA"})
        self.assertTrue(evaluation.reason_codes)
        self.assertEqual(GrowthTarget.objects.count(), 8)
        self.assertEqual(GrowthEvaluation.objects.count(), 1)

    def test_performance_and_reputation_use_observed_records(self):
        job = self._job("settled", profit="8", ppm="1", state=Job.State.SETTLED)
        worker = Worker.objects.create(id="profit-worker", worker_class="structured_data", version="1.0.0", status="READY")
        started = timezone.now() - timedelta(minutes=5)
        execution = Execution.objects.create(
            job=job, worker=worker, attempt=1, status="QA_PASSED", started_at=started,
            ended_at=timezone.now(), result={"operation": "json_to_csv"},
        )
        QAResult.objects.create(job=job, execution=execution, check_type="csv", passed=True, score="1")
        Payout.objects.create(
            job=job, gross="10", fee="1", net="9", state=Payout.State.SETTLED,
            settled_at=timezone.now(), currency="USD",
        )
        rows = refresh_performance(window_days=30)
        market_row = next(row for row in rows if row.dimension_type == "MARKET")
        capability_row = next(row for row in rows if row.dimension_type == "MARKET_CAPABILITY")
        self.assertEqual(market_row.settled_profit, Decimal("9.00"))
        self.assertEqual(capability_row.growth_stage, GrowthStage.BOOTSTRAP.value)
        self.assertGreater(market_row.profit_per_execution_minute, 0)

        reputation = record_reputation_snapshot(
            marketplace=self.market, source="market-api", rating="4.8", rating_count=12,
            completed_jobs=10, on_time_rate="0.95", revision_rate="0.10",
        )
        self.assertEqual(reputation.rating, Decimal("4.8"))
        self.assertEqual(ReputationSnapshot.objects.count(), 1)
        with self.assertRaises(ValueError):
            record_reputation_snapshot(marketplace=self.market, source="", rating="5")

    def test_opportunity_decisions_remain_auditable(self):
        job = self._job("audit")
        decision = evaluate_opportunity(job, capacity=self._capacity(), capability="structured_data")
        row = OpportunityDecision.objects.create(
            job=job, growth_stage=decision.growth_stage.value,
            utilization_state=decision.utilization_state.value, allowed=decision.allowed,
            expected_cash_profit=decision.expected_cash_profit,
            risk_adjusted_profit=decision.risk_adjusted_profit,
            reason_codes=list(decision.reason_codes),
        )
        self.assertEqual(row.job, job)
        self.assertEqual(row.growth_stage, "BOOTSTRAP")

    def test_agentgigs_scoring_persists_bounded_price_and_final_shadow_decision(self):
        market = Marketplace.objects.create(
            slug="agentgigs", display_name="AgentGigs", enabled=True,
            status=Marketplace.Status.LIVE, payout_ready=True, south_africa_verified=True,
            fee_rate="0.10",
        )
        MarketPolicyVersion.objects.create(
            marketplace=market, policy_hash="policy-current", automation_allowed=True, webdock_compatible=True,
        )
        job = Job.objects.create(
            marketplace=market, external_id="adaptive-price", title="Convert JSON to CSV",
            task_class="Data Analysis", reward="20", state=Job.State.DISCOVERED,
            normalized_payload={
                "operation": "json_to_csv", "source_filename": "input.json",
                "budget_min": 1000, "budget_max": 2000,
            },
        )
        green = type("Admission", (), {"allowed": True, "reason_codes": [], "id": "green"})()
        with patch.dict(os.environ, {"AUTONOMOUS_MODE": "SHADOW", "AGENTGIGS_AUTO_APPLY_ENABLED": "0"}, clear=False), patch(
            "control.services.acquisition_preflight.decide_admission", return_value=green,
        ):
            result = score_open_jobs(limit=10)
        self.assertEqual(result["scored"], 1)
        strategy = PricingStrategy.objects.get(marketplace=market)
        decision = OpportunityDecision.objects.get(job=job)
        self.assertGreaterEqual(strategy.offered_price, strategy.minimum_profitable_price)
        self.assertEqual(decision.pricing_strategy, strategy)
        self.assertFalse(decision.allowed)
        self.assertIn("AUTONOMY_SHADOW_ONLY", decision.reason_codes)
