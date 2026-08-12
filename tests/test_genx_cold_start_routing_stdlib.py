import unittest
from decimal import Decimal

from gateways.genx.contracts import ModelCandidate, route_models


class GenXColdStartRoutingTests(unittest.TestCase):
    def test_fresh_provider_model_routes_without_local_exploration_permission(self):
        routes = route_models(
            [ModelCandidate("fresh-text-model", expected_credits=Decimal("0.25"))],
            expected_revenue=Decimal("10"),
            required_quality=Decimal("0.80"),
            max_genx_credits=Decimal("1"),
            monetary_cost_per_credit=Decimal("0.01"),
            allow_exploration=False,
        )
        self.assertEqual([route.candidate.model_id for route in routes], ["fresh-text-model"])
        self.assertTrue(routes[0].exploration)

    def test_fresh_model_still_respects_the_real_credit_ceiling(self):
        routes = route_models(
            [ModelCandidate("too-expensive", expected_credits=Decimal("0.75"))],
            expected_revenue=Decimal("10"),
            max_genx_credits=Decimal("1"),
            monetary_cost_per_credit=Decimal("0.01"),
            allow_exploration=False,
        )
        self.assertEqual(routes, [])

    def test_fresh_model_still_respects_profitability(self):
        routes = route_models(
            [ModelCandidate("loss-maker", expected_credits=Decimal("0.25"))],
            expected_revenue=Decimal("0.001"),
            max_genx_credits=Decimal("1"),
            monetary_cost_per_credit=Decimal("1"),
            allow_exploration=False,
        )
        self.assertEqual(routes, [])

    def test_real_bad_history_can_reject_a_model_after_evidence_exists(self):
        routes = route_models(
            [
                ModelCandidate(
                    "proven-bad",
                    expected_credits=Decimal("0.25"),
                    attempts=4,
                    qa_accepted=0,
                    qa_rejected=4,
                )
            ],
            expected_revenue=Decimal("10"),
            required_quality=Decimal("0.80"),
            max_genx_credits=Decimal("1"),
            monetary_cost_per_credit=Decimal("0.01"),
        )
        self.assertEqual(routes, [])


if __name__ == "__main__":
    unittest.main()
