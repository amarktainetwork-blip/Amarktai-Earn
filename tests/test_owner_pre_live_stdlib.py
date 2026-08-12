import unittest
from decimal import Decimal
from pathlib import Path

from gateways.genx.contracts import ModelCandidate, route_models


class OwnerPreLiveContracts(unittest.TestCase):
    def test_product_publication_lock_does_not_join_nullable_offering(self):
        source = (Path(__file__).resolve().parents[1] / "control" / "services" / "product_factory.py").read_text(encoding="utf-8")
        self.assertIn('ProductCandidate.objects.select_for_update().get', source)
        self.assertNotIn('select_for_update().select_related("offering")', source)

    def test_economic_router_rejects_negative_expected_profit(self):
        model = ModelCandidate("expensive", price_hint=Decimal("20"), attempts=10, qa_accepted=10, credits=Decimal("200"))
        self.assertEqual(route_models(
            [model], expected_revenue=Decimal("5"), required_quality=Decimal("0.50"),
            max_genx_credits=Decimal("100"), monetary_cost_per_credit=Decimal("1"),
        ), [])

    def test_cold_start_is_provider_eligible_and_still_budget_bounded(self):
        model = ModelCandidate("new", price_hint=Decimal("0.01"))
        routed = route_models(
            [model], expected_revenue=Decimal("100"), max_genx_credits=Decimal("1"),
            allow_exploration=False,
        )
        self.assertTrue(routed and routed[0].exploration)

        oversized = ModelCandidate("oversized", price_hint=Decimal("0.75"))
        self.assertEqual(route_models(
            [oversized], expected_revenue=Decimal("100"), max_genx_credits=Decimal("1"),
            allow_exploration=False,
        ), [])


if __name__ == "__main__":
    unittest.main()