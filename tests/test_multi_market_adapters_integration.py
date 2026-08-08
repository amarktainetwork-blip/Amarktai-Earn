from __future__ import annotations

from django.test import TestCase

from control.models import Job, MarketIntegrationProfile, Marketplace, PayoutAccount
from control.ops import markets_snapshot
from control.services.markets import (
    bootstrap_market_integrations, refresh_verified_payout_gate, sync_market_discovery,
)
from markets.base import MarketCapabilities, NormalizedOpportunity


class FakeAdapter:
    slug = "callboard"
    capabilities = MarketCapabilities(discover=True)

    def health(self):
        return {"ok": True}

    def discover_jobs(self, **filters):
        return [{"id": "cb-1", "title": "Normalize source data", "reward": "12.50", "category": "data"}]

    def normalize_job(self, raw):
        return NormalizedOpportunity(raw["id"], raw["title"], raw["category"], __import__("decimal").Decimal(raw["reward"]), raw=raw)


class MultiMarketPersistenceTests(TestCase):
    def test_bootstrap_persists_exact_disabled_truth_and_is_idempotent(self):
        first = bootstrap_market_integrations()
        second = bootstrap_market_integrations()
        self.assertEqual(first["total"], 6)
        self.assertEqual(Marketplace.objects.count(), 6)
        self.assertEqual(MarketIntegrationProfile.objects.count(), 6)
        self.assertEqual(second["created"], 0)
        for market in Marketplace.objects.select_related("integration_profile"):
            self.assertFalse(market.enabled)
            self.assertFalse(market.payout_ready)
            self.assertFalse(market.south_africa_verified)
            self.assertFalse(market.integration_profile.autonomous_acquisition_enabled)
            self.assertTrue(market.integration_profile.blockers)
            self.assertEqual(set(market.integration_profile.capabilities), {
                "discover", "normalize", "claim", "apply", "bid", "messages", "input_assets",
                "submission", "revision", "status", "payment", "payout",
                "webhook_or_event_support", "rate_limit", "policy_verified", "payout_ready",
            })

    def test_crypto_payout_account_cannot_open_market_gate(self):
        bootstrap_market_integrations()
        market = Marketplace.objects.get(slug="taskbounty")
        PayoutAccount.objects.create(
            marketplace=market, rail="solana_usdc", status="READY", south_africa_verified=True,
        )
        self.assertFalse(refresh_verified_payout_gate(market))
        market.refresh_from_db()
        self.assertFalse(market.payout_ready)
        PayoutAccount.objects.create(
            marketplace=market, rail="USD_BANK_TRANSFER", status="READY", south_africa_verified=True,
        )
        self.assertTrue(refresh_verified_payout_gate(market))
        market.refresh_from_db()
        self.assertTrue(market.payout_ready)
        self.assertTrue(market.south_africa_verified)
        self.assertFalse(market.enabled)
        self.assertEqual(market.status, Marketplace.Status.PAYOUT_BLOCKED)

    def test_market_discovery_is_adapter_driven_without_enabling_acquisition(self):
        bootstrap_market_integrations()
        result = sync_market_discovery("callboard", adapter=FakeAdapter(), limit=10)
        self.assertEqual(result["discovered"], 1)
        job = Job.objects.get(marketplace__slug="callboard", external_id="cb-1")
        self.assertEqual(str(job.reward), "12.50")
        self.assertEqual(job.state, Job.State.DISCOVERED)
        self.assertFalse(job.marketplace.enabled)

    def test_market_dashboard_exposes_adapter_sources_capabilities_and_blockers(self):
        bootstrap_market_integrations()
        rows = markets_snapshot()["rows"]
        callboard = next(row for row in rows if row["market"] == "callboard")
        self.assertTrue(callboard["source_wired"])
        self.assertTrue(callboard["adapter_capabilities"]["submission"])
        self.assertFalse(callboard["adapter_acquisition_enabled"])
        self.assertIn("SOUTH_AFRICA_NON_CRYPTO_PAYOUT_NOT_VERIFIED", callboard["blockers"])
