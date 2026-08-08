import unittest
from decimal import Decimal
from control.acquisition import AcquisitionThresholds, MarketGate, ScoreGate, acquisition_gate


class AcquisitionGateTests(unittest.TestCase):
    def test_allows_only_live_payout_ready_profitable_market(self):
        decision = acquisition_gate(
            MarketGate(True, "LIVE", True, True),
            ScoreGate(Decimal("6.00"), Decimal("0.60"), Decimal("0.30")),
            AcquisitionThresholds(),
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason_codes, ())

    def test_blocks_payout_unverified_market_even_when_profitable(self):
        decision = acquisition_gate(
            MarketGate(True, "LIVE", False, False),
            ScoreGate(Decimal("20.00"), Decimal("2.00"), Decimal("0.10")),
            AcquisitionThresholds(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("PAYOUT_NOT_READY", decision.reason_codes)
        self.assertIn("SOUTH_AFRICA_NOT_VERIFIED", decision.reason_codes)

    def test_blocks_bad_economics(self):
        decision = acquisition_gate(
            MarketGate(True, "LIVE", True, True),
            ScoreGate(Decimal("0.50"), Decimal("0.01"), Decimal("3.00")),
            AcquisitionThresholds(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("EXPECTED_PROFIT_TOO_LOW", decision.reason_codes)
        self.assertIn("PROFIT_PER_MINUTE_TOO_LOW", decision.reason_codes)
        self.assertIn("GENX_BUDGET_TOO_HIGH", decision.reason_codes)


if __name__ == "__main__":
    unittest.main()
