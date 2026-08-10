import os
import unittest

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from control.services.payment_rails import (
    DEFAULT_PAYMENT_RAILS,
    RAIL_STATES,
    _merge_catalog,
    default_payment_rail_catalog,
    public_payment_rail_row,
)


class PaymentRailTruthTests(unittest.TestCase):
    def test_default_catalog_is_fail_closed_for_every_candidate_rail(self):
        catalog = default_payment_rail_catalog()
        self.assertEqual(set(catalog["rails"]), set(DEFAULT_PAYMENT_RAILS))
        for slug, row in catalog["rails"].items():
            with self.subTest(slug=slug):
                public = public_payment_rail_row(row)
                self.assertEqual(public["status"], "NOT_CONFIGURED")
                self.assertFalse(public["south_africa_verified"])
                self.assertFalse(public["checkout_enabled"])
                self.assertFalse(public["payout_receive_enabled"])
                self.assertFalse(public["final_settlement_enabled"])
                self.assertFalse(public["ready"])
                self.assertEqual(public["proof_reference"], "")

    def test_verified_label_is_not_ready_without_full_proof(self):
        catalog = default_payment_rail_catalog()
        row = catalog["rails"]["paypal"]
        row.update({"status": "VERIFIED", "south_africa_verified": True})
        self.assertFalse(public_payment_rail_row(row)["ready"])
        row["proof_reference"] = "owner-proof-1"
        self.assertFalse(public_payment_rail_row(row)["ready"])
        row["payout_receive_enabled"] = True
        self.assertTrue(public_payment_rail_row(row)["ready"])

    def test_persisted_unknown_status_fails_closed(self):
        merged = _merge_catalog({
            "rails": {
                "paystack": {
                    "status": "MAGIC_READY",
                    "south_africa_verified": True,
                    "proof_reference": "proof",
                    "checkout_enabled": True,
                }
            }
        })
        public = public_payment_rail_row(merged["rails"]["paystack"])
        self.assertEqual(public["status"], "BLOCKED")
        self.assertFalse(public["ready"])

    def test_public_rows_have_no_secret_fields(self):
        merged = _merge_catalog({
            "rails": {
                "wise": {
                    "status": "ACCOUNT_PROOF_REQUIRED",
                    "api_key": "must-not-leak",
                    "bank_account_number": "must-not-leak",
                    "proof_reference": "masked-proof",
                }
            }
        })
        public = public_payment_rail_row(merged["rails"]["wise"])
        self.assertNotIn("api_key", public)
        self.assertNotIn("bank_account_number", public)
        self.assertNotIn("must-not-leak", repr(public))
        self.assertEqual(public["proof_reference"], "masked-proof")

    def test_supported_states_are_explicit(self):
        self.assertEqual(
            set(RAIL_STATES),
            {
                "NOT_CONFIGURED",
                "ACCOUNT_PROOF_REQUIRED",
                "PENDING_EXTERNAL_APPROVAL",
                "VERIFIED",
                "BLOCKED",
                "PAUSED",
            },
        )


if __name__ == "__main__":
    unittest.main()
