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
    GenXCall,
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
    settled_profit_truth,
)
from control.services.agentgigs import score_open_jobs
from control.ops import overview_snapshot


class ProfitBrainIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(
            slug="profit-market", display_name="Profit Market", enabled=True,
            status=Marketplace.Status.LIVE, payout_ready=True, south_africa_verified=True,
        )

    def _job(
        self,
        external_id: str,
        *,
        profit="1.00",
        ppm="0.10",
        reward="2.00",
        genx_cost="0",
        external_cost="0",
        state=Job.State.EXPECTED,
        payload=None,
    ):
        job = Job.objects.create(
            marketplace=self.market, external_id=external_id, title="Profitable bounded task",
            task_class="Data Analysis", reward=reward, state=state,
            normalized_payload=payload or {"operation": "json_to_csv", "source_filename": "input.json"},
        )
        JobScore.objects.create(
            job=job, p_acquire="0.8", p_accept="0.9", p_payment="0.95",
            expected_genx_cost=genx_cost, expected_external_cost=external_cost,
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
        self.assertFalse(any("TARGET" in reason or "REVENUE" in reason for reason in decision.reason_codes))

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
        self.assertTrue(all(row.details["semantics"] == "OBJECTIVE_FLOOR_NEVER_EARNINGS_CAP" for row in targets))

    def test_bootstrap_and_exceeded_targets_never_cap_profitable_work(self):
        settled_job = self._job("target-proof", profit="500", ppm="50", reward="500", state=Job.State.SETTLED)
        Payout.objects.create(
            job=settled_job,
            gross="500",
            fee="0",
            net="500",
            state=Payout.State.SETTLED,
            settled_at=timezone.now(),
            currency="USD",
        )
        targets = ensure_growth_targets()
        GrowthTarget.objects.exclude(
            key__in=["TARGET_DAILY_SETTLED_PROFIT", "TARGET_WEEKLY_SETTLED_PROFIT"]
        ).update(enabled=False)
        GrowthTarget.objects.filter(
            key__in=["TARGET_DAILY_SETTLED_PROFIT", "TARGET_WEEKLY_SETTLED_PROFIT"]
        ).update(target_value="50")
        evaluation = evaluate_growth_targets()
        self.assertEqual(evaluation.status, "AHEAD")
        self.assertEqual(evaluation.metrics["TARGET_WEEKLY_SETTLED_PROFIT"], "500.00")

        next_job = self._job("bootstrap-uncapped", profit="400", ppm="40", reward="500")
        decision = evaluate_opportunity(next_job, capacity=self._capacity(), capability="new-bootstrap-capability")
        self.assertEqual(decision.growth_stage, GrowthStage.BOOTSTRAP)
        self.assertTrue(decision.allowed)
        self.assertGreater(decision.expected_cash_profit, Decimal("50"))
        self.assertFalse(any("TARGET" in reason or "REVENUE" in reason for reason in decision.reason_codes))
        self.assertTrue(all(row.details["semantics"] == "OBJECTIVE_FLOOR_NEVER_EARNINGS_CAP" for row in targets))

    def test_settled_profit_uses_actual_attributable_cost_once_and_respects_window(self):
        now = timezone.now()
        start = now - timedelta(days=1)
        first = self._job("truth-first", state=Job.State.SETTLED)
        second = self._job("truth-second", state=Job.State.SETTLED)
        pending = self._job("truth-pending", state=Job.State.PAYOUT_PENDING)
        Payout.objects.create(
            job=first, gross="100", fee="10", net="90", state=Payout.State.SETTLED,
            settled_at=now, currency="USD",
        )
        Payout.objects.create(
            job=second, gross="50", fee="5", net="45", state=Payout.State.SETTLED,
            settled_at=now, currency="USD",
        )
        Payout.objects.create(
            job=pending, gross="100", fee="10", net="90", state=Payout.State.PAYOUT_PENDING,
            pending_at=now, currency="USD",
        )
        GenXCall.objects.create(
            request_key="truth-cost-first", job=first, model="fixture", status="COMPLETED",
            completed_at=now, cost_equivalent="5",
        )
        GenXCall.objects.create(
            request_key="truth-cost-second", job=second, model="fixture", status="COMPLETED",
            completed_at=now, cost_equivalent="2",
        )
        GenXCall.objects.create(
            request_key="truth-cost-before-settlement-window", job=first, model="fixture", status="COMPLETED",
            # The payout settles in-window, so its valid job cost remains attributable
            # even though execution completed before the reporting window began.
            completed_at=start - timedelta(minutes=1), cost_equivalent="4",
        )
        GenXCall.objects.create(
            request_key="truth-cost-failed", job=first, model="fixture", status="FAILED",
            completed_at=now, cost_equivalent="20",
        )
        GenXCall.objects.create(
            request_key="truth-cost-unknown", job=first, model="fixture", status="UNKNOWN_REMOTE_STATE",
            cost_equivalent="20",
        )
        GenXCall.objects.create(
            request_key="truth-cost-unsettled", job=pending, model="fixture", status="COMPLETED",
            completed_at=now, cost_equivalent="10",
        )

        truth = settled_profit_truth(start=start, end=now + timedelta(days=1))

        self.assertEqual(truth.settled_cash, Decimal("135.00"))
        self.assertEqual(truth.paid_execution_cost, Decimal("11.00"))
        self.assertEqual(truth.net_settled_profit, Decimal("124.00"))
        self.assertEqual(truth.settled_payouts, 2)
        self.assertEqual(truth.costed_genx_calls, 3)
        self.assertIn("SETTLED_PAYOUT_NET_USD_ALREADY_EXCLUDES_MARKETPLACE_FEE", truth.coverage)
        self.assertIn("NO_PERSISTED_ACTUAL_EXTERNAL_OR_OTHER_DIRECT_COST_SOURCE", truth.coverage)
        self.assertFalse(truth.cost_coverage_complete)
        self.assertEqual(truth.unresolved_genx_cost_calls, 1)

    def test_settled_profit_attributes_pre_window_during_window_and_combined_job_costs(self):
        now = timezone.now()
        before_job = self._job("as-of-before", state=Job.State.SETTLED)
        Payout.objects.create(
            job=before_job, gross="100", fee="0", net="100", state=Payout.State.SETTLED,
            settled_at=now + timedelta(days=1), currency="USD",
        )
        GenXCall.objects.create(
            request_key="as-of-before-cost", job=before_job, model="fixture", status="COMPLETED",
            completed_at=now - timedelta(days=1), cost_equivalent="5",
        )
        before_truth = settled_profit_truth(start=now, end=now + timedelta(days=2))
        self.assertEqual(before_truth.paid_execution_cost, Decimal("5.00"))
        self.assertEqual(before_truth.net_settled_profit, Decimal("95.00"))

        during_job = self._job("as-of-during", state=Job.State.SETTLED)
        Payout.objects.create(
            job=during_job, gross="100", fee="0", net="100", state=Payout.State.SETTLED,
            settled_at=now + timedelta(days=4), currency="USD",
        )
        GenXCall.objects.create(
            request_key="as-of-during-cost", job=during_job, model="fixture", status="COMPLETED",
            completed_at=now + timedelta(days=3, hours=12), cost_equivalent="3",
        )
        during_truth = settled_profit_truth(start=now + timedelta(days=3), end=now + timedelta(days=5))
        self.assertEqual(during_truth.paid_execution_cost, Decimal("3.00"))
        self.assertEqual(during_truth.net_settled_profit, Decimal("97.00"))

        combined_job = self._job("as-of-combined", state=Job.State.SETTLED)
        Payout.objects.create(
            job=combined_job, gross="100", fee="0", net="100", state=Payout.State.SETTLED,
            settled_at=now + timedelta(days=7), currency="USD",
        )
        GenXCall.objects.create(
            request_key="as-of-combined-before", job=combined_job, model="fixture", status="COMPLETED",
            completed_at=now + timedelta(days=5), cost_equivalent="5",
        )
        GenXCall.objects.create(
            request_key="as-of-combined-during", job=combined_job, model="fixture", status="COMPLETED",
            completed_at=now + timedelta(days=6, hours=12), cost_equivalent="3",
        )
        combined_truth = settled_profit_truth(start=now + timedelta(days=6), end=now + timedelta(days=8))
        self.assertEqual(combined_truth.paid_execution_cost, Decimal("8.00"))
        self.assertEqual(combined_truth.net_settled_profit, Decimal("92.00"))
        self.assertTrue(combined_truth.cost_coverage_complete)

    def test_settled_profit_excludes_future_unrelated_and_unsettled_cost_and_counts_fee_once(self):
        now = timezone.now()
        start = now
        end = now + timedelta(days=2)
        settled = self._job("as-of-settled", state=Job.State.SETTLED)
        unrelated = self._job("as-of-unrelated", state=Job.State.SETTLED)
        pending = self._job("as-of-pending", state=Job.State.PAYOUT_PENDING)
        Payout.objects.create(
            job=settled, gross="100", fee="10", net="90", state=Payout.State.SETTLED,
            settled_at=now + timedelta(days=1), currency="USD",
        )
        Payout.objects.create(
            job=pending, gross="50", fee="5", net="45", state=Payout.State.PAYOUT_PENDING,
            pending_at=now + timedelta(days=1), currency="USD",
        )
        GenXCall.objects.create(
            request_key="as-of-known-cost", job=settled, model="fixture", status="COMPLETED",
            completed_at=start - timedelta(days=1), cost_equivalent="5",
        )
        future = GenXCall.objects.create(
            request_key="as-of-future-cost", job=settled, model="fixture", status="COMPLETED",
            completed_at=end + timedelta(seconds=1), cost_equivalent="50",
        )
        GenXCall.objects.filter(pk=future.pk).update(created_at=end + timedelta(seconds=1))
        GenXCall.objects.create(
            request_key="as-of-unrelated-cost", job=unrelated, model="fixture", status="COMPLETED",
            completed_at=now, cost_equivalent="30",
        )
        GenXCall.objects.create(
            request_key="as-of-unsettled-cost", job=pending, model="fixture", status="COMPLETED",
            completed_at=now, cost_equivalent="20",
        )

        truth = settled_profit_truth(start=start, end=end)

        self.assertEqual(truth.settled_cash, Decimal("90.00"))
        self.assertEqual(truth.paid_execution_cost, Decimal("5.00"))
        self.assertEqual(truth.net_settled_profit, Decimal("85.00"))
        self.assertTrue(truth.cost_coverage_complete)

    def test_unresolved_genx_monetary_cost_makes_profit_and_growth_coverage_incomplete(self):
        now = timezone.now()
        job = self._job("unresolved-cost", state=Job.State.SETTLED)
        Payout.objects.create(
            job=job, gross="100", fee="10", net="90", state=Payout.State.SETTLED,
            settled_at=now, currency="USD",
        )
        GenXCall.objects.create(
            request_key="unresolved-cost-call", job=job, model="fixture", status="COMPLETED",
            completed_at=now, credits="0", cost_equivalent="0", estimated_credits="0.25",
            requested_metadata={"billing_truth": "UNRESOLVED"},
        )

        truth = settled_profit_truth(start=now - timedelta(days=1), end=now + timedelta(days=1))
        growth = evaluate_growth_targets(persist=False)

        self.assertEqual(truth.net_settled_profit, Decimal("90.00"))
        self.assertFalse(truth.cost_coverage_complete)
        self.assertIn("ATTRIBUTABLE_GENX_MONETARY_COST_COVERAGE_INCOMPLETE", truth.coverage)
        self.assertEqual(growth.status, "INSUFFICIENT_DATA")
        self.assertIn("SETTLED_PROFIT_COST_COVERAGE_INCOMPLETE", growth.reason_codes)
        overview = overview_snapshot()
        labels = {row["label"]: row for row in overview["cards"]}
        incomplete_label = "RECORDED NET SETTLED PROFIT 30D — COST COVERAGE INCOMPLETE"
        self.assertIn(incomplete_label, labels)
        self.assertIn("RECORDED PAID EXECUTION COST 30D — COVERAGE INCOMPLETE", labels)
        self.assertEqual(labels["RECORDED NET MARGIN 30D"]["value"], "INSUFFICIENT_DATA")
        self.assertFalse(overview["meta"]["settled_profit_cost_coverage_complete"])

    def test_performance_and_reputation_use_observed_records(self):
        job = self._job("settled", profit="8", ppm="1", state=Job.State.SETTLED)
        worker = Worker.objects.create(id="profit-worker", worker_class="structured_data", version="1.0.0", status="READY")
        observed_at = timezone.now() - timedelta(seconds=1)
        started = observed_at - timedelta(minutes=5)
        execution = Execution.objects.create(
            job=job, worker=worker, attempt=1, status="QA_PASSED", started_at=started,
            ended_at=observed_at, result={"operation": "json_to_csv"},
        )
        QAResult.objects.create(job=job, execution=execution, check_type="csv", passed=True, score="1")
        Payout.objects.create(
            job=job, gross="10", fee="1", net="9", state=Payout.State.SETTLED,
            settled_at=observed_at, currency="USD",
        )
        performance_call = GenXCall.objects.create(
            request_key="performance-actual-cost", job=job, worker=worker, model="fixture",
            status="COMPLETED", completed_at=observed_at, cost_equivalent="2",
        )
        GenXCall.objects.filter(pk=performance_call.pk).update(created_at=observed_at)
        rows = refresh_performance(window_days=30)
        market_row = next(row for row in rows if row.dimension_type == "MARKET")
        capability_row = next(row for row in rows if row.dimension_type == "MARKET_CAPABILITY")
        self.assertEqual(market_row.settled_profit, Decimal("7.00"))
        self.assertEqual(market_row.genx_cost, Decimal("2"))
        self.assertEqual(market_row.direct_cost, Decimal("0"))
        self.assertIn("fee_not_subtracted_again", market_row.details["marketplace_fee_handling"])
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
