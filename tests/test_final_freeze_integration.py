from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from control.models import (
    BountyProgram, CapacitySnapshot, GrowthEvaluation, Job, Marketplace, MarketHealth, OpportunityDecision,
    OwnerSecurityProfile, Payout, PerformanceAggregate, ProgramScopeVersion, ReputationSnapshot,
)
from control.ops import snapshot
from control.services.v1_acceptance import build_acceptance_report


class ExpandedV1FreezeIntegrationTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            username="freeze-owner", password="long-final-freeze-password", is_staff=True,
        )
        OwnerSecurityProfile.objects.create(
            user=self.owner, totp_secret_encrypted="configured", totp_confirmed_at=timezone.now(),
        )
        self.market = Marketplace.objects.create(
            slug="freeze-market", display_name="Freeze Market", enabled=True,
            status=Marketplace.Status.LIVE, payout_ready=False, south_africa_verified=False,
        )
        MarketHealth.objects.create(marketplace=self.market, auth_ok=False, api_ok=True, supply_ok=True)
        self.job = Job.objects.create(
            marketplace=self.market, external_id="freeze-job", title="Final freeze job",
            task_class="analysis", reward="75.00", state=Job.State.AWARDED,
        )
        self.settled_job = Job.objects.create(
            marketplace=self.market, external_id="settled-job", title="Historically settled job",
            task_class="analysis", reward="50.00", state=Job.State.SETTLED,
        )
        now = timezone.now()
        Payout.objects.create(
            job=self.settled_job, gross="50.00", fee="5.00", net="45.00", state=Payout.State.SETTLED,
            earned_at=now, settled_at=now, external_reference="verified-fixture",
        )
        OpportunityDecision.objects.create(
            job=self.job, growth_stage="BOOTSTRAP", utilization_state="IDLE", allowed=False,
            expected_cash_profit="12.00", risk_adjusted_profit="9.00", reason_codes=["MARKET_PAYOUT_NOT_READY"],
        )
        CapacitySnapshot.objects.create(
            productive_slots=4, active_slots=1, available_slots=3, reserved_slots=0,
            utilization="0.25", utilization_state="MOSTLY_IDLE", profitable_eligible_waiting=2,
            avoidable_idle_minutes="15.00", unavoidable_idle_minutes="0", estimated_foregone_profit="6.00",
            idle_reason="ELIGIBLE_WORK_WAITING",
        )
        GrowthEvaluation.objects.create(
            status="BEHIND", window_start=now - timedelta(days=1), window_end=now,
            reason_codes=["PAYOUT_BLOCKED"], metrics={"settled_daily": "45"}, targets={"daily": "100"},
        )
        PerformanceAggregate.objects.create(
            dimension_type="CAPABILITY", dimension_key="analysis", marketplace=self.market,
            capability="analysis", growth_stage="BOOTSTRAP", window_start=now - timedelta(days=30), window_end=now,
            jobs_discovered=4, jobs_attempted=3, jobs_awarded=2, jobs_completed=1,
            qa_first_pass_rate="0.75", revision_rate="0.25", settlement_rate="0.50",
            gross_payout="50", platform_fees="5", settled_profit="40", runtime_seconds=120,
            profit_per_execution_minute="20", sample_count=4,
        )
        ReputationSnapshot.objects.create(
            marketplace=self.market, capability="analysis", rating="4.5", rating_count=12,
            completed_jobs=8, revision_rate="0.05", on_time_rate="0.95", source="market-fixture",
        )
        program = BountyProgram.objects.create(
            name="Expiring fixture scope", provider="fixture", status=BountyProgram.Status.ACTIVE,
            execution_enabled=True, automation_allowed=True,
        )
        ProgramScopeVersion.objects.create(
            program=program, version=1, authorization_hash="a" * 64,
            allowed_test_types=["PROMPT_INJECTION_EVALUATION"], rate_limit_per_minute=1,
            max_requests_per_attempt=1, max_spend_per_attempt="1", effective_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=2), active=True,
        )

    def test_overview_separates_cash_exposure_expected_profit_and_utilization(self):
        cards = {row["label"]: row for row in snapshot("overview", owner=self.owner)["cards"]}
        self.assertEqual(cards["SETTLED TODAY"]["value"], "$45.00")
        self.assertEqual(cards["SETTLED 7D"]["value"], "$45.00")
        self.assertEqual(cards["PAID EXECUTION COST 30D"]["value"], "$0.00")
        self.assertEqual(cards["TRUE RECORDED NET SETTLED PROFIT 30D"]["value"], "$45.00")
        self.assertEqual(cards["AWARDED/ACCEPTED EXPOSURE"]["value"], "$75.00")
        self.assertIn("not received cash", cards["AWARDED/ACCEPTED EXPOSURE"]["truth"])
        self.assertEqual(cards["RECORDED NET MARGIN 30D"]["value"], "90.00%")
        self.assertEqual(cards["TARGET STATUS"]["value"], "BEHIND")
        self.assertIn("never earnings caps", cards["TARGET STATUS"]["truth"])
        self.assertEqual(cards["PRODUCTIVE UTILIZATION"]["value"], "25.00%")
        self.assertEqual(cards["BLOCKED PROFITABLE OPPORTUNITIES 24H"]["value"], 1)

    def test_performance_and_market_pages_are_backed_by_persisted_economics(self):
        performance = snapshot("performance", owner=self.owner)
        row = next(item for item in performance["rows"] if item["key"] == "analysis")
        self.assertEqual(row["settled_profit"], "40.00")
        self.assertEqual(row["growth_stage"], "BOOTSTRAP")
        self.assertTrue(any(item.get("kind") == "reputation" and item.get("source") == "market-fixture" for item in performance["secondary_rows"]))
        market = snapshot("markets", owner=self.owner)["rows"][0]
        self.assertEqual(market["awards_total"], 2)
        self.assertEqual(market["settlements_total"], 1)
        self.assertEqual(market["settled_net"], "45.00")
        self.assertIn("PAYOUT_NOT_READY", market["blockers"])

    def test_alerts_derive_actionable_truth_without_exposing_secrets(self):
        alerts = snapshot("alerts", owner=self.owner)["rows"]
        types = {row["type"] for row in alerts}
        self.assertTrue({"AVOIDABLE_IDLE", "GROWTH_TARGET_BEHIND", "PAYOUT_BLOCKER", "MARKET_AUTH_FAILURE", "SAFETY_SCOPE_EXPIRY"}.issubset(types))
        self.assertNotIn("configured-secret", str(alerts))
        self.assertTrue(all(row["source"] in {"persisted", "database-derived"} for row in alerts))

    def test_expanded_acceptance_has_no_repository_solvable_fail(self):
        redis_client = Mock(); redis_client.ping.return_value = True
        with patch("control.services.v1_acceptance.redis.Redis.from_url", return_value=redis_client):
            report = build_acceptance_report(ci_proven=True)
        self.assertEqual(report["counts"]["FAIL"], 0)
        by_id = {row["id"]: row for row in report["criteria"]}
        for identifier in (
            "growth_governor", "utilization_economics", "adaptive_economic_learning", "multifile_composite",
            "expanded_worker_qa", "synthetic_data_factory", "authorized_safety_research", "multi_market_adapters",
            "dashboard_economic_truth", "uncapped_profit_governor",
        ):
            self.assertEqual(by_id[identifier]["status"], "PASS", by_id[identifier])
        self.assertEqual(by_id["live_market_account"]["status"], "EXTERNAL_PROOF_REQUIRED")
