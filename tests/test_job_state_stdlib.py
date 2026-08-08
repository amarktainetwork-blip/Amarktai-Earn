import unittest
from control.job_state import InvalidJobTransition, assert_transition


class JobStateTests(unittest.TestCase):
    def test_happy_path(self):
        path = ["DISCOVERED", "EXPECTED", "CLAIMED", "EXECUTING", "SUBMITTED", "ACCEPTED", "PAYOUT_PENDING", "SETTLED"]
        for current, target in zip(path, path[1:]):
            assert_transition(current, target)

    def test_revision_reopens_execution(self):
        assert_transition("SUBMITTED", "EXECUTING")

    def test_cannot_turn_discovered_job_into_settled_cash(self):
        with self.assertRaises(InvalidJobTransition):
            assert_transition("DISCOVERED", "SETTLED")


if __name__ == "__main__":
    unittest.main()
