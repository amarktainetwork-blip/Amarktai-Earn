import unittest
from decimal import Decimal
from control.acquisition import AcquisitionThresholds, MarketGate, ScoreGate, acquisition_gate, paid_cost_envelope


class AcquisitionGateTests(unittest.TestCase):
    def test_allows_only_live_payout_ready_profitable_market(self):
        decision = acquisition_gate(
            MarketGate(True, "LIVE", True, True),
            ScoreGate(Decimal("6.00"), Decimal("0.60"), Decimal("0.30"), Decimal("10.00"), Decimal("1.00")),
            AcquisitionThresholds(),
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason_codes, ())

    def test_blocks_payout_unverified_market_even_when_profitable(self):
        decision = acquisition_gate(
            MarketGate(True, "LIVE", False, False),
            ScoreGate(Decimal("20.00"), Decimal("2.00"), Decimal("0.10"), Decimal("30.00"), Decimal("3.00")),
            AcquisitionThresholds(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("PAYOUT_NOT_READY", decision.reason_codes)
        self.assertIn("SOUTH_AFRICA_NOT_VERIFIED", decision.reason_codes)

    def test_blocks_bad_economics(self):
        decision = acquisition_gate(
            MarketGate(True, "LIVE", True, True),
            ScoreGate(
                Decimal("-0.50"),
                Decimal("-0.05"),
                Decimal("9.50"),
                Decimal("10.00"),
                Decimal("1.00"),
            ),
            AcquisitionThresholds(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("EXPECTED_PROFIT_TOO_LOW", decision.reason_codes)
        self.assertIn("PROFIT_PER_MINUTE_TOO_LOW", decision.reason_codes)
        self.assertIn("EXPECTED_NET_PROFIT_NOT_POSITIVE", decision.reason_codes)
        self.assertIn("RISK_ADJUSTED_PROFIT_NOT_POSITIVE", decision.reason_codes)

    def test_high_value_profit_supports_paid_cost_above_legacy_fixed_caps(self):
        envelope = paid_cost_envelope(
            expected_gross=Decimal("500"),
            marketplace_fee=Decimal("50"),
            expected_genx_cost=Decimal("20"),
            expected_external_cost=Decimal("0"),
            expected_operational_cost=Decimal("0.10"),
            risk_adjusted_profit=Decimal("400"),
            absolute_max_paid_cost=Decimal("250"),
        )
        self.assertTrue(envelope.allowed)
        self.assertEqual(envelope.expected_paid_cost, Decimal("20.10"))
        self.assertEqual(envelope.approved_paid_cost_budget, Decimal("22.110"))
        self.assertGreater(envelope.expected_net_profit, 0)

    def test_emergency_paid_cost_ceiling_bounds_runaway_downside(self):
        envelope = paid_cost_envelope(
            expected_gross=Decimal("1000"),
            marketplace_fee=Decimal("100"),
            expected_genx_cost=Decimal("300"),
            expected_external_cost=Decimal("0"),
            expected_operational_cost=Decimal("0"),
            risk_adjusted_profit=Decimal("500"),
            absolute_max_paid_cost=Decimal("250"),
        )
        self.assertFalse(envelope.allowed)
        self.assertIn("PAID_COST_EMERGENCY_CEILING_EXCEEDED", envelope.reason_codes)


if __name__ == "__main__":
    unittest.main()
