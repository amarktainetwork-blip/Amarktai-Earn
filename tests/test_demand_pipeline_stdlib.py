import os
import unittest
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from control.services.demand_pipeline import (
    BUYER_DEMAND,
    INCOMPLETE_REQUIREMENTS,
    SELLER_SUPPLY_LISTING,
    UNFUNDED,
    UNKNOWN,
    qualify_payload,
)


class DemandQualificationTests(unittest.TestCase):
    def test_structured_buyer_job_with_requirements_is_actionable(self):
        result = qualify_payload(
            {
                "type": "job",
                "description": "Normalize the supplied CSV file.",
                "requirements": ["Return a normalized CSV"],
                "funded": True,
            },
            title="Normalize customer data",
            reward=Decimal("25"),
        )
        self.assertEqual(result.classification, BUYER_DEMAND)
        self.assertTrue(result.actionable)

    def test_seller_service_advertisement_never_becomes_buyer_demand(self):
        result = qualify_payload(
            {
                "type": "service_listing",
                "description": "I offer AI automation and reporting services for businesses.",
            },
            title="AI agent available for projects",
            reward=Decimal("100"),
        )
        self.assertEqual(result.classification, SELLER_SUPPLY_LISTING)
        self.assertFalse(result.actionable)

    def test_unfunded_truth_wins_over_buyer_language(self):
        result = qualify_payload(
            {
                "type": "job",
                "description": "Create a report from the supplied dataset.",
                "acceptanceCriteria": [{"id": "c1", "description": "Report delivered"}],
                "posterFunded": False,
            },
            title="Create report",
            reward=Decimal("50"),
        )
        self.assertEqual(result.classification, UNFUNDED)
        self.assertFalse(result.actionable)

    def test_explicit_missing_inputs_fail_closed(self):
        result = qualify_payload(
            {
                "type": "job",
                "description": "Analyze the source file.",
                "missingInputs": ["source.csv"],
            },
            title="Analyze source file",
            reward=Decimal("40"),
        )
        self.assertEqual(result.classification, INCOMPLETE_REQUIREMENTS)
        self.assertFalse(result.actionable)

    def test_ambiguous_inventory_fails_closed(self):
        result = qualify_payload(
            {"description": "Data and reporting expertise."},
            title="Data work",
            reward=Decimal("30"),
        )
        self.assertEqual(result.classification, UNKNOWN)
        self.assertFalse(result.actionable)
        self.assertIn("DEMAND_CLASSIFICATION_AMBIGUOUS", result.reason_codes)


if __name__ == "__main__":
    unittest.main()
