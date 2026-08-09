import os
import unittest
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from control.services.profit_brain import (
    GrowthStage,
    TargetStatus,
    UtilizationState,
    classify_growth_stage,
    classify_utilization,
    discovery_limit,
    recommend_price,
)


class ProfitBrainDeterministicTests(unittest.TestCase):
    def test_growth_stages_require_samples_reliability_and_profit(self):
        self.assertEqual(classify_growth_stage(sample_count=1, completed_jobs=1, settled_profit=Decimal("100"), qa_rate=Decimal("1"), settlement_rate=Decimal("1")), GrowthStage.BOOTSTRAP)
        self.assertEqual(classify_growth_stage(sample_count=10, completed_jobs=5, settled_profit=Decimal("20"), qa_rate=Decimal("1"), settlement_rate=Decimal("1")), GrowthStage.ESTABLISH)
        self.assertEqual(classify_growth_stage(sample_count=30, completed_jobs=20, settled_profit=Decimal("20"), qa_rate=Decimal("0.95"), settlement_rate=Decimal("0.90")), GrowthStage.PROFIT)
        self.assertEqual(classify_growth_stage(sample_count=60, completed_jobs=30, settled_profit=Decimal("20"), qa_rate=Decimal("0.95"), settlement_rate=Decimal("0.90")), GrowthStage.SCALE)

    def test_utilization_states_and_discovery_are_capacity_responsive(self):
        self.assertEqual(classify_utilization(0, 4)[0], UtilizationState.IDLE)
        self.assertEqual(classify_utilization(1, 4)[0], UtilizationState.MOSTLY_IDLE)
        self.assertEqual(classify_utilization(2, 4)[0], UtilizationState.PARTIALLY_IDLE)
        self.assertEqual(classify_utilization(4, 4)[0], UtilizationState.BUSY)
        self.assertEqual(discovery_limit(100, UtilizationState.BUSY), 50)
        self.assertEqual(discovery_limit(100, UtilizationState.IDLE), 200)

    def test_pricing_is_bounded_and_never_below_profitable_floor(self):
        low_win = recommend_price(
            total_expected_cost=Decimal("4.00"), advertised_budget=Decimal("20"), competitive_price=Decimal("10"),
            fee_rate=Decimal("0.10"), utilization_state=UtilizationState.IDLE,
            growth_stage=GrowthStage.BOOTSTRAP, historical_win_rate=Decimal("0.10"),
        )
        high_win = recommend_price(
            total_expected_cost=Decimal("4.00"), advertised_budget=Decimal("20"), competitive_price=Decimal("10"),
            fee_rate=Decimal("0.10"), utilization_state=UtilizationState.BUSY,
            growth_stage=GrowthStage.PROFIT, historical_win_rate=Decimal("0.90"),
        )
        self.assertGreaterEqual(low_win.offered_price, low_win.minimum_profitable_price)
        self.assertGreater(high_win.offered_price, low_win.offered_price)
        self.assertLessEqual(abs(high_win.adjustment_fraction), Decimal("0.20"))

    def test_status_vocabulary_is_explicit(self):
        self.assertEqual({item.value for item in TargetStatus}, {"AHEAD", "ON_TRACK", "BEHIND", "INSUFFICIENT_DATA"})

    def test_targets_and_growth_stage_are_not_acquisition_or_revenue_caps(self):
        root = Path(__file__).resolve().parents[1]
        decision_sources = "\n".join(
            (root / relative).read_text(encoding="utf-8")
            for relative in (
                "control/acquisition.py",
                "control/services/acquisition_preflight.py",
                "control/services/jobs.py",
            )
        )
        self.assertNotIn("TARGET_DAILY_SETTLED_PROFIT", decision_sources)
        self.assertNotIn("TARGET_WEEKLY_SETTLED_PROFIT", decision_sources)
        self.assertNotIn("TARGET_COMPLETED_JOBS_DAY", decision_sources)
        self.assertNotIn("target_value", decision_sources)

        env_example = (root / ".env.example").read_text(encoding="utf-8")
        self.assertIn("TARGET_* values are objective floors", env_example)
        self.assertIn("ABSOLUTE_MAX_PAID_COST_PER_JOB_USD=250.00", env_example)
        self.assertNotIn("MAX_EXECUTION_COST_PER_JOB_USD", env_example)
        self.assertNotIn("MAX_GENX_COST_PER_JOB_USD", env_example)


if __name__ == "__main__":
    unittest.main()
