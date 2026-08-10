import hashlib
import hmac
import json
import os
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from control.models import InboundOrder, MarketServiceListing, Marketplace
from control.services.channel_ingress import ChannelIngressError
from control.services.channel_onboarding import update_priority_channel_onboarding
from control.services.lemon_webhooks import dispatch_lemon_webhook
from markets.revenue_catalog import bootstrap_revenue_market_catalog


class ChannelCompletionRegressionIntegrationTests(TestCase):
    def setUp(self):
        bootstrap_revenue_market_catalog()
        call_command("bootstrap_channel_packages", verbosity=0)

    def test_revoking_onboarding_proof_removes_managed_seller_capabilities_and_policy(self):
        ready = update_priority_channel_onboarding(
            "rapidapi",
            checks={
                "provider_account_configured": True,
                "provider_contract_verified": True,
                "service_terms_verified": True,
            },
            proof_reference="fixture-account-contract-and-terms-proof",
            actor="test-owner",
        )
        self.assertTrue(ready["account_contract_ready"])
        market = Marketplace.objects.get(slug="rapidapi")
        profile = market.integration_profile
        self.assertTrue(profile.seller_capabilities["publish_service"])
        self.assertTrue(profile.seller_capabilities["receive_orders"])
        self.assertTrue(profile.policy_verified)
        self.assertTrue(market.policy_versions.order_by("-checked_at", "-created_at").first().automation_allowed)

        blocked = update_priority_channel_onboarding(
            "rapidapi",
            checks={
                "provider_account_configured": False,
                "provider_contract_verified": False,
                "service_terms_verified": False,
            },
            proof_reference="",
            actor="test-owner",
        )
        self.assertFalse(blocked["account_contract_ready"])
        market.refresh_from_db()
        profile.refresh_from_db()
        self.assertFalse(profile.seller_capabilities["publish_service"])
        self.assertFalse(profile.seller_capabilities["receive_orders"])
        self.assertFalse(profile.seller_capabilities["usage_metering"])
        self.assertFalse(profile.policy_verified)
        self.assertEqual(profile.automation_status, "BLOCKED")
        self.assertFalse(market.policy_versions.order_by("-checked_at", "-created_at").first().automation_allowed)

    def test_lemon_rejects_custom_package_slug_that_does_not_match_purchased_variant(self):
        purchased = MarketServiceListing.objects.get(offering__slug="lemon-seo-subscription")
        purchased.status = MarketServiceListing.Status.PUBLISHED
        purchased.remote_listing_id = "variant-seo-1"
        purchased.remote_reference = "https://example.invalid/lemon/variant-seo-1"
        purchased.published_at = timezone.now()
        purchased.save(update_fields=["status", "remote_listing_id", "remote_reference", "published_at", "updated_at"])

        other = MarketServiceListing.objects.get(offering__slug="lemon-research-product")
        other.status = MarketServiceListing.Status.PUBLISHED
        other.remote_listing_id = "variant-research-1"
        other.remote_reference = "https://example.invalid/lemon/variant-research-1"
        other.published_at = timezone.now()
        other.save(update_fields=["status", "remote_listing_id", "remote_reference", "published_at", "updated_at"])

        payload = {
            "meta": {
                "event_name": "order_created",
                "custom_data": {
                    "package_slug": "lemon-research-product",
                    "requirements": {},
                },
            },
            "data": {
                "id": "order-mismatch-1",
                "type": "orders",
                "attributes": {
                    "status": "paid",
                    "currency": "USD",
                    "total": 4900,
                    "tax": 0,
                    "first_order_item": {"variant_id": "variant-seo-1"},
                },
            },
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        secret = "fixture-lemon-secret"
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {"LEMON_SQUEEZY_WEBHOOK_SECRET": secret, "LEMON_SQUEEZY_WEBHOOK_ENABLED": "1"}, clear=False):
            with self.assertRaisesRegex(ChannelIngressError, "LEMON_PACKAGE_VARIANT_MISMATCH"):
                dispatch_lemon_webhook(raw_body=raw, signature=signature)
        self.assertEqual(InboundOrder.objects.filter(marketplace__slug="lemon-squeezy").count(), 0)
