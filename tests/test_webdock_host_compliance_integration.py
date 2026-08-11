from __future__ import annotations

import os
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from control.models import MarketHealth, Marketplace
from control.services.host_policy import (
    WEBDOCK_OFFHOST_ONLY_MARKETS,
    market_runtime_compatible,
    runtime_policy_errors,
)
from control.services.market_readiness import market_readiness
from control.services.workload_policy import evaluate_text
from markets.revenue_catalog import BY_SLUG as REVENUE_BY_SLUG, bootstrap_revenue_market_catalog


class WebdockHostComplianceIntegrationTests(TestCase):
    def test_current_webdock_prohibited_workloads_fail_closed(self):
        cases = {
            "Run an Ethereum validator node on the VPS": "PROHIBITED_BLOCKCHAIN_RUNTIME",
            "Deploy a Bitcoin testnet node on the server": "PROHIBITED_BLOCKCHAIN_RUNTIME",
            "Install XMRig for crypto mining": "PROHIBITED_CRYPTO_MINING",
            "Run a DePIN node for passive rewards": "PROHIBITED_DEPIN",
            "Perform an nmap port scan of the customer network": "PROHIBITED_UNAUTHORIZED_SCANNING",
            "Run a load test against the public endpoint": "PROHIBITED_STRESS_TESTING",
            "Operate a Tor relay on this host": "PROHIBITED_TOR_RELAY",
            "Seed torrents from the VPS": "PROHIBITED_TORRENT",
            "Host a live media streaming server": "PROHIBITED_MEDIA_STREAMING_SERVER",
            "Continuously scrape third party sites 24/7": "PROHIBITED_CONTINUOUS_SCRAPING",
            "Scrape copyrighted paywalled articles": "PROHIBITED_COPYRIGHT_SCRAPING",
            "Mirror pirated movies for distribution": "PROHIBITED_COPYRIGHT_DISTRIBUTION",
            "Run a traffic exchange autosurf service": "PROHIBITED_TRAFFIC_EXCHANGE",
            "Host an LLM locally on the VPS": "PROHIBITED_LOCAL_INFERENCE",
            "Fine-tune a neural network locally on the server": "PROHIBITED_LOCAL_INFERENCE",
            "Send bulk unsolicited email to scraped leads": "PROHIBITED_SPAM",
            "Sell unused bandwidth as a residential proxy resale service": "PROHIBITED_BANDWIDTH_RESALE",
        }
        for text, reason in cases.items():
            with self.subTest(text=text):
                decision = evaluate_text(text)
                self.assertFalse(decision.allowed)
                self.assertIn(reason, decision.reason_codes)

    def test_legitimate_business_work_and_offhost_crypto_payment_are_not_false_positives(self):
        allowed = (
            "Write a research report about Bitcoin adoption in South Africa.",
            "Build a normal REST API and deploy the web application.",
            "Process this supplied MP4 into a smaller downloadable video file.",
            "Extract the authorised public product page once while respecting robots.txt.",
            "Prepare an invoice where settlement may later arrive as USDC through an external payment provider.",
            "Review this supplied repository for defensive code quality and security issues.",
            "Draft customer support replies without sending them.",
        )
        for text in allowed:
            with self.subTest(text=text):
                self.assertTrue(evaluate_text(text).allowed, evaluate_text(text).reason_codes)

    def test_all_onchain_candidates_are_catalogued_offhost_only(self):
        self.assertEqual(
            WEBDOCK_OFFHOST_ONLY_MARKETS,
            {
                slug for slug, definition in REVENUE_BY_SLUG.items()
                if definition.hosting_policy == "OFFHOST_SETTLEMENT_REQUIRED"
            },
        )
        for slug in WEBDOCK_OFFHOST_ONLY_MARKETS:
            definition = REVENUE_BY_SLUG[slug]
            self.assertEqual(definition.execution_placement, "OFFHOST_REQUIRED")
            self.assertIn("WALLET_EXECUTION_PROHIBITED_ON_WEBDOCK", definition.blockers)
            self.assertFalse(market_runtime_compatible(slug, provider="webdock"))

    def test_offhost_market_cannot_be_made_webdock_ready_by_database_mutation(self):
        bootstrap_revenue_market_catalog()
        market = Marketplace.objects.get(slug="virtuals-acp")
        policy = market.policy_versions.order_by("-checked_at", "-created_at").first()
        policy.webdock_compatible = True
        policy.automation_allowed = True
        policy.save(update_fields=["webdock_compatible", "automation_allowed", "updated_at"])
        profile = market.integration_profile
        profile.policy_verified = True
        profile.source_wired = True
        profile.capabilities = {"discover": True}
        profile.blockers = []
        profile.save(update_fields=["policy_verified", "source_wired", "capabilities", "blockers", "updated_at"])
        MarketHealth.objects.update_or_create(
            marketplace=market,
            defaults={"api_ok": True, "auth_ok": True, "payout_ok": True, "supply_ok": True},
        )
        readiness = market_readiness(market)
        self.assertFalse(readiness["work_ready"])
        self.assertIn("MARKET_RUNTIME_NOT_COMPATIBLE", readiness["work_blockers"])

    def test_webdock_runtime_tripwires_reject_prohibited_feature_switches(self):
        with patch.dict(os.environ, {"HOST_PROVIDER": "webdock", "CRYPTO_NODE_ENABLED": "1", "NETWORK_SCANNER_ENABLED": "true"}, clear=False):
            errors = runtime_policy_errors()
        self.assertIn("CRYPTO_NODE_ENABLED cannot be enabled on Webdock", errors)
        self.assertIn("NETWORK_SCANNER_ENABLED cannot be enabled on Webdock", errors)

    def test_production_check_consumes_host_policy_errors(self):
        with patch.dict(os.environ, {"AMARKTAI_ENV": "production"}, clear=False), patch(
            "control.management.commands.production_check.runtime_policy_errors",
            return_value=["CRYPTO_NODE_ENABLED cannot be enabled on Webdock"],
        ):
            with self.assertRaises(CommandError) as raised:
                call_command("production_check")
        self.assertIn("CRYPTO_NODE_ENABLED cannot be enabled on Webdock", str(raised.exception))
