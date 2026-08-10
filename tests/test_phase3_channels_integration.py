import json
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from control.jwt_auth import issue_access
from control.models import MarketServiceListing, Marketplace, OwnerSecurityProfile, ServiceOffering, SystemSetting
from control.services.channel_launch import priority_channel_launch_snapshot
from control.services.channel_packages import PACKAGE_SPECS, priority_channel_package_snapshot
from control.services.settlement_routes import SETTLEMENT_ROUTE_SETTING_KEY
from markets.revenue_catalog import bootstrap_revenue_market_catalog


class Phase3ChannelIntegrationTests(TestCase):
    def setUp(self):
        bootstrap_revenue_market_catalog()
        self.owner = get_user_model().objects.create_user(
            username="phase3-owner",
            password="Strong-Phase3-Owner-Password-2026!",
            is_staff=True,
        )
        OwnerSecurityProfile.objects.create(user=self.owner)

    def authenticate(self):
        self.client.cookies["amarktai_access"] = issue_access(self.owner)

    def test_bootstrap_command_materializes_priority_channels_fail_closed_and_is_idempotent(self):
        priority = {"contra", "rapidapi", "apify-store", "lemon-squeezy"}
        Marketplace.objects.filter(slug__in=priority).delete()
        self.assertFalse(Marketplace.objects.filter(slug__in=priority).exists())

        first_out = StringIO()
        call_command("bootstrap_revenue_catalog", stdout=first_out)
        first = json.loads(first_out.getvalue().strip().splitlines()[-1])
        self.assertGreaterEqual(first["created"], 4)

        markets = Marketplace.objects.filter(slug__in=priority)
        self.assertEqual(set(markets.values_list("slug", flat=True)), priority)
        for market in markets:
            self.assertFalse(market.enabled)
            self.assertFalse(market.payout_ready)
            self.assertFalse(market.south_africa_verified)
            self.assertEqual(market.status, Marketplace.Status.PAYOUT_BLOCKED)
            self.assertIsNotNone(market.integration_profile)

        plan = priority_channel_launch_snapshot()
        self.assertEqual(plan["meta"]["shadow_preparation_ready"], 4)
        self.assertEqual(plan["meta"]["activation_ready"], 0)
        self.assertFalse(plan["meta"]["external_mutation_allowed"])

        second_out = StringIO()
        call_command("bootstrap_revenue_catalog", stdout=second_out)
        second = json.loads(second_out.getvalue().strip().splitlines()[-1])
        self.assertEqual(second["created"], 0)
        self.assertEqual(Marketplace.objects.filter(slug__in=priority).count(), 4)

    def test_priority_channel_packages_materialize_as_local_drafts_and_are_idempotent(self):
        package_slugs = {spec.slug for spec in PACKAGE_SPECS}

        first_out = StringIO()
        call_command("bootstrap_channel_packages", stdout=first_out)
        first = json.loads(first_out.getvalue().strip().splitlines()[-1])
        self.assertEqual(first["packages"], len(PACKAGE_SPECS))
        self.assertEqual(first["offerings_created"], len(PACKAGE_SPECS))
        self.assertEqual(first["listings_created"], len(PACKAGE_SPECS))

        offerings = ServiceOffering.objects.filter(slug__in=package_slugs)
        self.assertEqual(offerings.count(), len(PACKAGE_SPECS))
        self.assertFalse(offerings.filter(enabled=True).exists())
        self.assertFalse(offerings.filter(accepting_orders=True).exists())

        listings = MarketServiceListing.objects.filter(offering__slug__in=package_slugs).select_related("offering", "marketplace")
        self.assertEqual(listings.count(), len(PACKAGE_SPECS))
        self.assertFalse(listings.exclude(status=MarketServiceListing.Status.DRAFT).exists())
        self.assertFalse(listings.exclude(remote_listing_id="").exists())
        self.assertFalse(listings.exclude(remote_reference="").exists())

        rapidapi = list(listings.filter(marketplace__slug="rapidapi"))
        self.assertEqual(len(rapidapi), 6)
        self.assertTrue(all(row.pricing_model == ServiceOffering.PricingModel.PER_CALL for row in rapidapi))
        self.assertTrue(all(str(row.offering.platform_fee_rate) == "0.250000" for row in rapidapi))
        self.assertTrue(all(row.published_price > 0 for row in rapidapi))

        apify = listings.get(offering__slug="apify-website-data-extractor")
        self.assertEqual(apify.pricing_model, ServiceOffering.PricingModel.PER_UNIT)
        self.assertEqual(str(apify.published_price), "0.00")
        self.assertIn(
            "EXTERNAL_EXECUTION_COST_PROFILE_NOT_PROVEN",
            apify.platform_metadata["channel_package"]["pricing_blockers"],
        )
        self.assertEqual(apify.platform_metadata["channel_package"]["execution_placement"], "APIFY")

        lemon_subscription = listings.get(offering__slug="lemon-seo-subscription")
        self.assertEqual(lemon_subscription.pricing_model, ServiceOffering.PricingModel.SUBSCRIPTION)
        self.assertGreater(lemon_subscription.published_price, 0)

        package_snapshot = priority_channel_package_snapshot()
        self.assertEqual(package_snapshot["meta"]["total_packages"], len(PACKAGE_SPECS))
        self.assertEqual(package_snapshot["meta"]["prepared_packages"], len(PACKAGE_SPECS))
        self.assertEqual(package_snapshot["meta"]["price_ready_packages"], len(PACKAGE_SPECS) - 1)
        self.assertEqual(package_snapshot["meta"]["published_packages"], 0)
        self.assertFalse(package_snapshot["meta"]["external_mutation_allowed"])
        self.assertTrue(all(row["external_mutation_allowed"] is False for row in package_snapshot["rows"]))

        launch = priority_channel_launch_snapshot()
        self.assertEqual(launch["meta"]["total_packages"], len(PACKAGE_SPECS))
        self.assertEqual(launch["meta"]["prepared_packages"], len(PACKAGE_SPECS))
        self.assertEqual(launch["meta"]["activation_ready"], 0)
        self.assertEqual(
            {row["market"]: row["package_count"] for row in launch["rows"]},
            {"contra": 5, "rapidapi": 6, "apify-store": 1, "lemon-squeezy": 5},
        )

        second_out = StringIO()
        call_command("bootstrap_channel_packages", stdout=second_out)
        second = json.loads(second_out.getvalue().strip().splitlines()[-1])
        self.assertEqual(second["offerings_created"], 0)
        self.assertEqual(second["listings_created"], 0)
        self.assertEqual(second["unchanged"], len(PACKAGE_SPECS))
        self.assertEqual(MarketServiceListing.objects.filter(offering__slug__in=package_slugs).count(), len(PACKAGE_SPECS))

    def test_banking_snapshot_includes_fail_closed_market_settlement_routes(self):
        self.authenticate()
        response = self.client.get("/api/banking/rails")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("settlement_routes", body)
        self.assertIn("settlement_route_meta", body)
        rapidapi = next(row for row in body["settlement_routes"] if row["market"] == "rapidapi")
        self.assertEqual(rapidapi["candidate_rails"], ["paypal"])
        self.assertEqual(rapidapi["status"], "UNMAPPED")
        self.assertFalse(rapidapi["ready"])
        self.assertIn("OWNER_PAYMENT_RAIL_NOT_SELECTED", rapidapi["blockers"])

    @patch("control.banking_views.verify_reauthentication", return_value=True)
    def test_route_can_be_proposed_but_not_verified_before_owner_rail_is_ready(self, _verify):
        self.authenticate()
        endpoint = "/api/banking/routes/rapidapi/proof"
        proposed = self.client.post(
            endpoint,
            data={
                "status": "PROPOSED",
                "selected_rail": "paypal",
                "proof_reference": "planned-provider-route",
                "notes": "No activation claim",
                "password": "fixture",
                "code": "123456",
            },
            content_type="application/json",
        )
        self.assertEqual(proposed.status_code, 200)
        route = proposed.json()["route"]
        self.assertEqual(route["status"], "PROPOSED")
        self.assertFalse(route["ready"])
        self.assertIn("OWNER_PAYMENT_RAIL_NOT_READY", route["blockers"])

        verified = self.client.post(
            endpoint,
            data={
                "status": "VERIFIED",
                "selected_rail": "paypal",
                "proof_reference": "planned-provider-route",
                "password": "fixture",
                "code": "123456",
            },
            content_type="application/json",
        )
        self.assertEqual(verified.status_code, 400)
        self.assertIn("settlement_route_not_ready", verified.json()["error"])

        setting = SystemSetting.objects.get(key=SETTLEMENT_ROUTE_SETTING_KEY)
        persisted = repr(setting.value)
        self.assertNotIn("fixture", persisted)
        self.assertNotIn("123456", persisted)

    def test_priority_channel_launch_api_is_owner_only_and_shadow_only(self):
        unauthenticated = self.client.get("/api/channels/priority-launch")
        self.assertEqual(unauthenticated.status_code, 401)

        self.authenticate()
        response = self.client.get("/api/channels/priority-launch")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            {row["market"] for row in body["rows"]},
            {"contra", "rapidapi", "apify-store", "lemon-squeezy"},
        )
        self.assertEqual(body["meta"]["activation_ready"], 0)
        self.assertFalse(body["meta"]["external_mutation_allowed"])
        self.assertEqual(body["meta"]["shadow_preparation_ready"], 4)
        for row in body["rows"]:
            self.assertTrue(row["shadow_preparation_ready"])
            self.assertFalse(row["activation_ready"])
            self.assertFalse(row["external_mutation_allowed"])
            self.assertIn("VERIFIED_OWNER_SETTLEMENT_ROUTE_REQUIRED", row["external_blockers"])
