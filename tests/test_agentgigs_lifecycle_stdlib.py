import unittest
from decimal import Decimal

from control.agentgigs_lifecycle import (
    application_is_awarded,
    authoritative_payout_from_earnings,
    details_decision,
    webhook_decision,
)


class AgentGigsLifecycleTests(unittest.TestCase):
    def test_only_funded_application_is_treated_as_awarded(self):
        self.assertFalse(application_is_awarded("pending"))
        self.assertFalse(application_is_awarded("accepted"))
        self.assertTrue(application_is_awarded("funded"))

    def test_details_maps_assigned_funded_to_awarded_and_delivered_to_submitted(self):
        funded = details_decision({"job": {"status": "in_progress"}, "isAssigned": True, "myApplication": {"status": "funded"}})
        self.assertTrue(funded.awarded)
        delivered = details_decision({"job": {"status": "delivered"}, "isAssigned": True, "myApplication": {"status": "funded"}})
        self.assertTrue(delivered.submitted)

    def test_webhook_payment_events_are_distinct(self):
        self.assertTrue(webhook_decision("job.revision_requested").revision_required)
        self.assertTrue(webhook_decision("job.approved").approved)
        self.assertTrue(webhook_decision("payment.released").payment_released)
        self.assertFalse(webhook_decision("job.accepted").approved)

    def test_earnings_calculator_is_authoritative_only_when_math_matches(self):
        result = authoritative_payout_from_earnings({"calculator": {"jobAmount": 5000, "commissionAmount": 500, "agentPayout": 4500}})
        self.assertEqual(result, (Decimal("50"), Decimal("5"), Decimal("45")))
        self.assertIsNone(authoritative_payout_from_earnings({"calculator": {"jobAmount": 5000, "commissionAmount": 500, "agentPayout": 4400}}))


if __name__ == "__main__":
    unittest.main()
