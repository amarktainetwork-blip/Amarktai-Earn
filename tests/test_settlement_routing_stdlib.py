import unittest

from control.settlement_rules import settlement_route_blockers, settlement_route_ready


class SettlementRouteRuleTests(unittest.TestCase):
    def _rail(self, **overrides):
        row = {
            "ready": True,
            "south_africa_verified": True,
            "payout_receive_enabled": True,
            "final_settlement_enabled": False,
        }
        row.update(overrides)
        return row

    def test_unmapped_route_is_blocked_even_when_market_flags_are_ready(self):
        blockers = settlement_route_blockers(
            route_status="UNMAPPED",
            selected_rail="",
            proof_reference="",
            market_payout_ready=True,
            market_south_africa_verified=True,
            rail=None,
            candidate_rails=("paypal",),
        )
        self.assertIn("SETTLEMENT_ROUTE_NOT_VERIFIED", blockers)
        self.assertIn("OWNER_PAYMENT_RAIL_NOT_SELECTED", blockers)
        self.assertIn("SETTLEMENT_ROUTE_PROOF_REQUIRED", blockers)

    def test_verified_route_requires_ready_receiving_rail(self):
        blockers = settlement_route_blockers(
            route_status="VERIFIED",
            selected_rail="paypal",
            proof_reference="owner-proof",
            market_payout_ready=True,
            market_south_africa_verified=True,
            rail=self._rail(ready=False, payout_receive_enabled=False),
            candidate_rails=("paypal",),
        )
        self.assertIn("OWNER_PAYMENT_RAIL_NOT_READY", blockers)
        self.assertIn("OWNER_PAYMENT_RAIL_CANNOT_RECEIVE_SETTLEMENT", blockers)

    def test_candidate_mismatch_blocks_route(self):
        blockers = settlement_route_blockers(
            route_status="VERIFIED",
            selected_rail="wise",
            proof_reference="route-proof",
            market_payout_ready=True,
            market_south_africa_verified=True,
            rail=self._rail(),
            candidate_rails=("paypal",),
        )
        self.assertIn("OWNER_PAYMENT_RAIL_NOT_IN_MARKET_CANDIDATES", blockers)

    def test_fully_verified_route_is_ready(self):
        kwargs = dict(
            route_status="VERIFIED",
            selected_rail="paypal",
            proof_reference="route-proof",
            market_payout_ready=True,
            market_south_africa_verified=True,
            rail=self._rail(),
            candidate_rails=("paypal",),
        )
        self.assertEqual(settlement_route_blockers(**kwargs), ())
        self.assertTrue(settlement_route_ready(**kwargs))


if __name__ == "__main__":
    unittest.main()
