from __future__ import annotations

import os
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from control.management.commands.run_revenue_watcher import Command as RevenueWatcherCommand
from control.models import MarketHealth, Marketplace
from control.services.market_readiness import market_readiness
from control.services.markets import bootstrap_market_integrations


class MarketReadinessDomainTests(TestCase):
    def setUp(self):
        bootstrap_market_integrations()

    def _make_connected_live(self, slug: str):
        market = Marketplace.objects.select_related("integration_profile").get(slug=slug)
        market.enabled = True
        market.status = Marketplace.Status.LIVE
        market.save(update_fields=["enabled", "status", "updated_at"])
        MarketHealth.objects.update_or_create(
            marketplace=market,
            defaults={
                "api_ok": True,
                "auth_ok": True,
                "payout_ok": False,
                "supply_ok": True,
                "last_error_code": "",
                "checked_at": timezone.now(),
                "details": {"test": True},
            },
        )
        return market

    def test_dealwork_can_be_live_test_ready_while_cash_remains_unready(self):
        market = self._make_connected_live("dealwork")
        profile = market.integration_profile
        profile.blockers = [
            code for code in profile.blockers
            if code != "DEALWORK_KYA_NOT_VERIFIED"
        ]
        profile.autonomous_acquisition_enabled = True
        profile.save(update_fields=["blockers", "autonomous_acquisition_enabled", "updated_at"])

        with patch.dict(os.environ, {
            "AUTONOMOUS_MODE": "LOW_RISK",
            "DEALWORK_AUTO_ACQUIRE_ENABLED": "1",
        }, clear=False):
            row = market_readiness(market)

        self.assertTrue(row["work_ready"])
        self.assertTrue(row["platform_wallet_proving"])
        self.assertTrue(row["live_test_ready"])
        self.assertFalse(row["cash_ready"])
        self.assertEqual(row["cash_blockers"], ["PAYOUT_NOT_READY", "SOUTH_AFRICA_NOT_VERIFIED"])
        self.assertTrue(row["autonomy_ready"])

    def test_non_wallet_market_still_requires_cash_route_for_live_proving(self):
        market = self._make_connected_live("callboard")
        profile = market.integration_profile
        profile.blockers = [
            code for code in profile.blockers
            if code != "AGENT_OWNER_CLAIM_NOT_VERIFIED"
        ]
        profile.autonomous_acquisition_enabled = True
        profile.save(update_fields=["blockers", "autonomous_acquisition_enabled", "updated_at"])

        with patch.dict(os.environ, {
            "AUTONOMOUS_MODE": "LOW_RISK",
            "CALLBOARD_AUTO_ACQUIRE_ENABLED": "1",
        }, clear=False):
            row = market_readiness(market)

        self.assertTrue(row["work_ready"])
        self.assertFalse(row["platform_wallet_proving"])
        self.assertFalse(row["live_test_ready"])
        self.assertIn("CASH_ROUTE_REQUIRED_FOR_LIVE_PROVING", row["live_test_blockers"])
        self.assertFalse(row["cash_ready"])
        self.assertFalse(row["autonomy_ready"])

    def test_dealwork_kya_is_a_work_blocker_not_a_banking_blocker(self):
        market = self._make_connected_live("dealwork")
        row = market_readiness(market)
        self.assertFalse(row["work_ready"])
        self.assertIn("DEALWORK_KYA_NOT_VERIFIED", row["work_blockers"])
        self.assertNotIn("PAYOUT_NOT_READY", row["work_blockers"])
        self.assertIn("PAYOUT_NOT_READY", row["cash_blockers"])


class DealworkWatcherTests(TestCase):
    def test_disabled_dealwork_watcher_is_inert(self):
        with patch.dict(os.environ, {"DEALWORK_WATCHER_ENABLED": "0"}, clear=False):
            result = RevenueWatcherCommand()._dealwork_cycle(limit=10)
        self.assertEqual(result, {"enabled": False, "mutation_performed": False})

    def test_enabled_dealwork_watcher_only_runs_shadow_discovery(self):
        with patch.dict(os.environ, {"DEALWORK_WATCHER_ENABLED": "1"}, clear=False), patch(
            "control.services.markets.sync_market_discovery",
            return_value={
                "market": "dealwork",
                "discovered": 3,
                "buyer_demand": 2,
                "scored": 2,
                "qualification_counts": {"BUYER_DEMAND": 2, "UNFUNDED": 1},
                "jobs_total": 3,
            },
        ) as sync:
            result = RevenueWatcherCommand()._dealwork_cycle(limit=10)

        sync.assert_called_once_with("dealwork", limit=10)
        self.assertTrue(result["enabled"])
        self.assertFalse(result["mutation_performed"])
        self.assertEqual(result["buyer_demand"], 2)
        self.assertEqual(result["scored"], 2)
