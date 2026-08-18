from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from control.jwt_auth import issue_access
from control.models import AuditEvent, OwnerSecurityProfile, SystemSetting
from control.services.payment_rails import PAYMENT_RAIL_SETTING_KEY


class BankingIntegrationTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            username="banking-owner",
            password="Strong-Banking-Owner-Password-2026!",
            is_staff=True,
        )
        OwnerSecurityProfile.objects.create(user=self.owner)

    def authenticate(self):
        self.client.cookies["amarktai_access"] = issue_access(self.owner)

    def test_banking_page_and_api_are_owner_only(self):
        page = self.client.get("/ops/banking/")
        self.assertEqual(page.status_code, 302)
        self.assertEqual(page.url, "/login/")
        api = self.client.get("/api/banking/rails")
        self.assertEqual(api.status_code, 401)

    def test_authenticated_treasury_page_exposes_fail_closed_receipt_rails(self):
        self.authenticate()
        page = self.client.get("/ops/banking/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'data-section="treasury"')
        self.assertContains(page, "Balances and the advanced accounting ledger")
        self.assertContains(page, "control/app.js")

        response = self.client.get("/api/banking/rails")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["section"], "treasury")
        self.assertEqual(body["meta"]["ready_rails"], 0)
        self.assertEqual(body["meta"]["action_required"], 6)
        self.assertEqual(body["meta"]["accounts_open_now"], 16)
        self.assertEqual(body["meta"]["accounts_open_next"], 0)
        self.assertEqual(body["meta"]["optional_accounts"], 2)
        self.assertEqual(
            {row["slug"] for row in body["rows"]},
            {"paystack", "paypal", "provider-bank", "stripe-connect", "wise", "payoneer"},
        )
        self.assertTrue(all(row["ready"] is False for row in body["rows"]))
        self.assertTrue(all(row["stores_bank_details"] is False for row in body["rows"]))
        self.assertTrue(all(row["stores_private_keys"] is False for row in body["rows"]))
        self.assertTrue(next(row for row in body["rows"] if row["slug"] == "paypal")["human_withdrawal_required"])
        self.assertFalse(next(row for row in body["rows"] if row["slug"] == "paystack")["human_withdrawal_required"])
        self.assertEqual(
            [row["slug"] for row in body["account_setup"]["open_now"]],
            [
                "paystack", "paypal", "lemon-squeezy", "payhip", "ko-fi", "gumroad", "patreon",
                "rapidapi", "apify-store", "taskbounty", "contra", "freelancer", "dealwork", "algora",
                "impact", "partnerstack",
            ],
        )
        self.assertEqual(body["account_setup"]["open_next"], [])
        self.assertEqual(
            [row["slug"] for row in body["account_setup"]["optional"]],
            ["wise", "payoneer"],
        )
        self.assertNotIn("opire", {row["slug"] for row in body["account_setup"]["open_now"]})
        self.assertNotIn("agentgigs", {row["slug"] for row in body["account_setup"]["open_now"]})
        self.assertNotIn("callboard", {row["slug"] for row in body["account_setup"]["open_now"]})

    @patch("control.banking_views.verify_reauthentication", return_value=True)
    def test_verified_state_requires_sa_proof_reference_and_live_capability(self, _verify):
        self.authenticate()
        endpoint = "/api/banking/rails/paypal/proof"

        missing_sa = self.client.post(
            endpoint,
            data={
                "status": "VERIFIED",
                "proof_reference": "paypal-owner-proof",
                "payout_receive_enabled": True,
                "password": "correct",
                "code": "123456",
            },
            content_type="application/json",
        )
        self.assertEqual(missing_sa.status_code, 400)
        self.assertEqual(missing_sa.json()["error"], "south_africa_proof_required")

        missing_capability = self.client.post(
            endpoint,
            data={
                "status": "VERIFIED",
                "south_africa_verified": True,
                "proof_reference": "paypal-owner-proof",
                "password": "correct",
                "code": "123456",
            },
            content_type="application/json",
        )
        self.assertEqual(missing_capability.status_code, 400)
        self.assertEqual(missing_capability.json()["error"], "live_payment_capability_required")

        verified = self.client.post(
            endpoint,
            data={
                "status": "VERIFIED",
                "south_africa_verified": True,
                "payout_receive_enabled": True,
                "proof_reference": "paypal-owner-proof",
                "owner_action": "Human withdrawal can happen later outside AmarktAI",
                "password": "correct",
                "code": "123456",
            },
            content_type="application/json",
        )
        self.assertEqual(verified.status_code, 200)
        row = verified.json()["rail"]
        self.assertTrue(row["ready"])
        self.assertTrue(row["south_africa_verified"])
        self.assertTrue(row["payout_receive_enabled"])
        self.assertTrue(row["human_withdrawal_required"])
        self.assertFalse(row["stores_bank_details"])
        self.assertFalse(row["stores_private_keys"])
        self.assertEqual(row["proof_reference"], "paypal-owner-proof")

        setting = SystemSetting.objects.get(key=PAYMENT_RAIL_SETTING_KEY)
        self.assertFalse(setting.sensitive)
        self.assertNotIn("password", repr(setting.value).lower())
        self.assertNotIn("123456", repr(setting.value))

        audit = AuditEvent.objects.filter(event_type="treasury.payment_rail_proof_updated").latest("created_at")
        self.assertEqual(audit.metadata["rail"], "paypal")
        self.assertTrue(audit.metadata["ready"])
        self.assertTrue(audit.metadata["human_withdrawal_required"])
        self.assertNotIn("password", repr(audit.metadata).lower())
        self.assertNotIn("123456", repr(audit.metadata))

    @patch("control.banking_views.verify_reauthentication", return_value=False)
    def test_failed_reauthentication_never_changes_payment_rail_truth(self, _verify):
        self.authenticate()
        response = self.client.post(
            "/api/banking/rails/wise/proof",
            data={
                "status": "VERIFIED",
                "south_africa_verified": True,
                "payout_receive_enabled": True,
                "proof_reference": "wise-proof",
                "password": "wrong",
                "code": "000000",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(SystemSetting.objects.filter(key=PAYMENT_RAIL_SETTING_KEY).exists())
        self.assertTrue(AuditEvent.objects.filter(event_type="treasury.payment_rail_update_reauth_failed").exists())
