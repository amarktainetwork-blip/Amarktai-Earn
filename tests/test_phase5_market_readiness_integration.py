from __future__ import annotations

import os
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from control.management.commands.run_revenue_watcher import Command as RevenueWatcherCommand
from control.models import MarketHealth, Marketplace
from control.services.dealwork_runtime import attempt_dealwork_bids
from control.services.market_readiness import (
    acquisition_cash_gate_required,
    acquisition_profile_blockers,
    market_readiness,
)
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
        self.assertFalse(acquisition_cash_gate_required(market))

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
        self.assertTrue(acquisition_cash_gate_required(market))

    def test_dealwork_kya_is_a_work_blocker_not_a_banking_blocker(self):
        market = self._make_connected_live("dealwork")
        row = market_readiness(market)
        self.assertFalse(row["work_ready"])
        self.assertIn("DEALWORK_KYA_NOT_VERIFIED", row["work_blockers"])
        self.assertNotIn("PAYOUT_NOT_READY", row["work_blockers"])
        self.assertIn("PAYOUT_NOT_READY", row["cash_blockers"])

    def test_dealwork_acquisition_filters_only_reviewed_deferred_blockers(self):
        market = Marketplace.objects.get(slug="dealwork")
        blockers = [
            "DEALWORK_KYA_NOT_VERIFIED",
            "WITHDRAWAL_RAIL_NOT_VERIFIED",
            "ACCOUNT_PAYOUT_NOT_VERIFIED",
            "SOUTH_AFRICA_NON_CRYPTO_PAYOUT_NOT_VERIFIED",
            "SERVICE_LISTING_CONTRACT_NOT_PROVED",
            "UNEXPECTED_NEW_SAFETY_BLOCKER",
        ]
        filtered = acquisition_profile_blockers(market, blockers)
        self.assertEqual(filtered, [
            "DEALWORK_KYA_NOT_VERIFIED",
            "UNEXPECTED_NEW_SAFETY_BLOCKER",
        ])

    def test_dealwork_bid_cycle_is_inert_until_all_autonomy_gates_are_armed(self):
        market = self._make_connected_live("dealwork")
        profile = market.integration_profile
        profile.blockers = [code for code in profile.blockers if code != "DEALWORK_KYA_NOT_VERIFIED"]
        profile.save(update_fields=["blockers", "updated_at"])

        with patch.dict(os.environ, {
            "AUTONOMOUS_MODE": "OFF",
            "DEALWORK_AUTO_ACQUIRE_ENABLED": "0",
        }, clear=False), patch("control.services.dealwork_runtime.configured_adapter") as adapter:
            result = attempt_dealwork_bids()

        adapter.assert_not_called()
        self.assertFalse(result["enabled"])
        self.assertFalse(result["mutation_performed"])
        self.assertIn("AUTONOMY_NOT_MUTATING", result["reason_codes"])


class DealworkWatcherTests(TestCase):
    def test_disabled_dealwork_watcher_is_inert(self):
        with patch.dict(os.environ, {"DEALWORK_WATCHER_ENABLED": "0"}, clear=False):
            result = RevenueWatcherCommand()._dealwork_cycle(limit=10)
        self.assertEqual(result, {"enabled": False, "mutation_performed": False})

    def test_enabled_dealwork_watcher_uses_bounded_runtime_cycle(self):
        expected = {
            "enabled": True,
            "discovery": {"discovered": 3, "mutation_performed": False},
            "acquisition": {"submitted": 0, "mutation_performed": False},
            "mutation_performed": False,
        }
        with patch.dict(os.environ, {"DEALWORK_WATCHER_ENABLED": "1"}, clear=False), patch(
            "control.services.dealwork_runtime.run_dealwork_cycle",
            return_value=expected,
        ) as cycle:
            result = RevenueWatcherCommand()._dealwork_cycle(limit=10)

        cycle.assert_called_once_with(limit=10)
        self.assertEqual(result, expected)
        self.assertFalse(result["mutation_performed"])
