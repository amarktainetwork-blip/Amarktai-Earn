import unittest

from control.payout_state import InvalidPayoutTransition, assert_payout_transition


class PayoutStateTests(unittest.TestCase):
    def test_happy_path_to_settled(self):
        assert_payout_transition(None, "EARNED")
        assert_payout_transition("EARNED", "PAYOUT_PENDING")
        assert_payout_transition("PAYOUT_PENDING", "SETTLED")

    def test_cannot_count_unearned_money_as_settled(self):
        with self.assertRaises(InvalidPayoutTransition):
            assert_payout_transition(None, "SETTLED")

    def test_reversal_is_explicit(self):
        assert_payout_transition("SETTLED", "REVERSED")
        with self.assertRaises(InvalidPayoutTransition):
            assert_payout_transition("REVERSED", "SETTLED")


if __name__ == "__main__":
    unittest.main()
