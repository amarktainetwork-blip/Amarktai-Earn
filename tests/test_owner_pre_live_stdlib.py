import unittest
from decimal import Decimal

from gateways.genx.contracts import ModelCandidate, route_models


class OwnerPreLiveContracts(unittest.TestCase):
    def test_economic_router_rejects_negative_expected_profit(self):
        model = ModelCandidate("expensive", price_hint=Decimal("20"), attempts=10, qa_accepted=10, credits=Decimal("200"))
        self.assertEqual(route_models([model], expected_revenue=Decimal("5"), required_quality=Decimal("0.50")), [])

    def test_exploration_is_explicit_and_bounded(self):
        model = ModelCandidate("new", price_hint=Decimal("0.01"))
        self.assertEqual(route_models([model], expected_revenue=Decimal("100"), allow_exploration=False), [])
        explored = route_models([model], expected_revenue=Decimal("100"), allow_exploration=True, exploration_fraction=Decimal("0.05"))
        self.assertTrue(explored and explored[0].exploration)


if __name__ == "__main__":
    unittest.main()
