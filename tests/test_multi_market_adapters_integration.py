from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from control.models import AcquisitionPreflight, Job, JobScore, MarketIntegrationProfile, Marketplace, PayoutAccount
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
        return [{
            "id": "cb-1",
            "title": "Normalize source data",
            "reward": "12.50",
            "category": "data",
            "type": "job",
            "operation": "csv_normalize",
            "requirements": ["Normalize the supplied CSV and return the cleaned file"],
            "funded": True,
        }]

    def normalize_job(self, raw):
        return NormalizedOpportunity(raw["id"], raw["title"], raw["category"], Decimal(raw["reward"]), raw=raw)


class SparseBuyerAdapter(FakeAdapter):
    def __init__(self, external_id):
        self.external_id = external_id

    def discover_jobs(self, **filters):
        return [{
            "id": self.external_id,
            "title": "Normalize source data",
            "reward": "12.50",
            "category": "data",
            "operation": "csv_normalize",
        }]


class SellerListingAdapter(FakeAdapter):
    def discover_jobs(self, **filters):
        return [{
            "id": "cb-seller-1",
            "title": "AI reporting agent available",
            "reward": "50.00",
            "category": "data",
            "type": "service_listing",
            "description": "I offer reporting and automation services.",
        }]


class SellerRefreshAdapter(FakeAdapter):
    def discover_jobs(self, **filters):
        return [{
            "id": "cb-1",
            "title": "AI reporting agent available",
            "reward": "12.50",
            "category": "data",
            "type": "service_listing",
            "description": "I offer reporting and automation services.",
        }]


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

    def test_market_discovery_qualifies_scores_and_preflights_without_enabling_acquisition(self):
        bootstrap_market_integrations()
        result = sync_market_discovery("callboard", adapter=FakeAdapter(), limit=10)
        self.assertEqual(result["discovered"], 1)
        self.assertEqual(result["buyer_demand"], 1)
        self.assertEqual(result["scored"], 1)
        self.assertEqual(result["qualification_counts"], {"BUYER_DEMAND": 1})
        job = Job.objects.get(marketplace__slug="callboard", external_id="cb-1")
        self.assertEqual(str(job.reward), "12.50")
        self.assertEqual(job.state, Job.State.EXPECTED)
        self.assertEqual(job.normalized_payload["demand_qualification"]["classification"], "BUYER_DEMAND")
        self.assertEqual(job.jobscore.decision, "WATCH")
        self.assertTrue(job.acquisition_preflights.exists())
        self.assertFalse(job.acquisition_preflights.latest("created_at").allowed)
        self.assertFalse(job.marketplace.enabled)

    def test_trusted_adapter_provenance_qualifies_sparse_job_and_bounty_rows(self):
        bootstrap_market_integrations()
        bounty_markets = {"taskbounty", "opire", "algora"}
        for slug in ("callboard", "taskbounty", "opire", "algora"):
            external_id = f"{slug}-sparse-1"
            result = sync_market_discovery(slug, adapter=SparseBuyerAdapter(external_id), limit=10)
            self.assertEqual(result["buyer_demand"], 1, slug)
            self.assertEqual(result["scored"], 1, slug)
            job = Job.objects.get(marketplace__slug=slug, external_id=external_id)
            expected_source_type = "bounty" if slug in bounty_markets else "posted_job"
            self.assertEqual(job.normalized_payload["_amarktai_source_type"], expected_source_type)
            self.assertEqual(job.normalized_payload["_amarktai_source_market"], slug)
            self.assertEqual(job.normalized_payload["demand_qualification"]["classification"], "BUYER_DEMAND")
            self.assertEqual(job.state, Job.State.EXPECTED)

    def test_seller_listing_is_filtered_before_scoring(self):
        bootstrap_market_integrations()
        result = sync_market_discovery("callboard", adapter=SellerListingAdapter(), limit=10)
        self.assertEqual(result["discovered"], 1)
        self.assertEqual(result["buyer_demand"], 0)
        self.assertEqual(result["scored"], 0)
        self.assertEqual(result["qualification_counts"], {"SELLER_SUPPLY_LISTING": 1})
        job = Job.objects.get(marketplace__slug="callboard", external_id="cb-seller-1")
        self.assertEqual(job.state, Job.State.DISCOVERED)
        self.assertEqual(job.normalized_payload["demand_qualification"]["classification"], "SELLER_SUPPLY_LISTING")
        self.assertFalse(JobScore.objects.filter(job=job).exists())

    def test_refresh_to_non_actionable_invalidates_stale_score_and_preflight(self):
        bootstrap_market_integrations()
        sync_market_discovery("callboard", adapter=FakeAdapter(), limit=10)
        job = Job.objects.get(marketplace__slug="callboard", external_id="cb-1")
        self.assertEqual(job.state, Job.State.EXPECTED)
        self.assertTrue(JobScore.objects.filter(job=job).exists())
        self.assertTrue(AcquisitionPreflight.objects.filter(job=job).exists())

        result = sync_market_discovery("callboard", adapter=SellerRefreshAdapter(), limit=10)
        self.assertEqual(result["qualification_counts"], {"SELLER_SUPPLY_LISTING": 1})
        self.assertEqual(result["scored"], 0)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.DISCOVERED)
        self.assertEqual(job.normalized_payload["demand_qualification"]["classification"], "SELLER_SUPPLY_LISTING")
        self.assertFalse(JobScore.objects.filter(job=job).exists())
        stale_preflights = AcquisitionPreflight.objects.filter(job=job)
        self.assertTrue(stale_preflights.exists())
        self.assertFalse(stale_preflights.filter(eligible=True).exists())
        self.assertFalse(stale_preflights.filter(allowed=True).exists())
        self.assertTrue(all("DEMAND_NO_LONGER_ACTIONABLE" in row.reason_codes for row in stale_preflights))

    def test_market_dashboard_exposes_adapter_sources_capabilities_and_blockers(self):
        bootstrap_market_integrations()
        rows = markets_snapshot()["rows"]
        callboard = next(row for row in rows if row["market"] == "callboard")
        self.assertTrue(callboard["source_wired"])
        self.assertTrue(callboard["adapter_capabilities"]["submission"])
        self.assertFalse(callboard["adapter_acquisition_enabled"])
        self.assertIn("SOUTH_AFRICA_NON_CRYPTO_PAYOUT_NOT_VERIFIED", callboard["blockers"])
