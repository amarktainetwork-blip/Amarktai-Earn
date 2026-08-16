from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from control.services.market_priority import (
    ACTIVE_MARKETS,
    ARCHIVED_MARKETS,
    CANONICAL_EARNING_MARKETS,
    INACTIVE_MARKETS,
    PRIORITIES,
)


class MarketPriorityStdlibTests(unittest.TestCase):
    def test_priority_covers_exact_canonical_earning_catalog(self):
        self.assertEqual(set(PRIORITIES), set(CANONICAL_EARNING_MARKETS))
        self.assertEqual(len(CANONICAL_EARNING_MARKETS), 18)
        self.assertEqual(
            ACTIVE_MARKETS,
            {
                "taskbounty", "rapidapi", "apify-store", "algora", "opire",
                "gitpay", "lemon-squeezy", "contra", "dealwork",
            },
        )
        self.assertEqual(len(ACTIVE_MARKETS), 9)
        self.assertEqual(ARCHIVED_MARKETS, {"agentmarket", "chowdr"})
        self.assertEqual(len(INACTIVE_MARKETS), 7)
        self.assertFalse(ACTIVE_MARKETS & ARCHIVED_MARKETS)
        self.assertFalse(ACTIVE_MARKETS & INACTIVE_MARKETS)
        self.assertFalse(ARCHIVED_MARKETS & INACTIVE_MARKETS)
        self.assertEqual(ACTIVE_MARKETS | ARCHIVED_MARKETS | INACTIVE_MARKETS, CANONICAL_EARNING_MARKETS)
        self.assertNotIn("internal-genx-proof", CANONICAL_EARNING_MARKETS)

    def test_activation_order_starts_with_usable_owner_receipt_routes(self):
        ordered = sorted(PRIORITIES.items(), key=lambda item: item[1].rank)
        self.assertEqual(
            [slug for slug, _priority in ordered[:5]],
            ["taskbounty", "rapidapi", "apify-store", "algora", "opire"],
        )
        self.assertTrue(all(priority.tier in {"ACTIVATE_FIRST", "OWNER_ACTION"} for _slug, priority in ordered[:5]))
        self.assertEqual([priority.rank for _slug, priority in ordered], list(range(1, 19)))

    def test_unusable_or_unimplemented_owner_payout_routes_are_not_active(self):
        for slug in ("agentgigs", "callboard"):
            self.assertNotIn(slug, ACTIVE_MARKETS)
            self.assertIn(slug, INACTIVE_MARKETS)
            priority = PRIORITIES[slug]
            self.assertEqual(priority.payout_autonomy_score, 0)
            self.assertEqual(priority.south_africa_setup_score, 0)
            self.assertIn("Stripe", priority.payout_path)

        self.assertNotIn("nevermined", ACTIVE_MARKETS)
        self.assertIn("nevermined", INACTIVE_MARKETS)
        self.assertIn("Fiat", PRIORITIES["nevermined"].payout_path)
        self.assertEqual(PRIORITIES["nevermined"].action, "VERIFY_FIAT_PLAN_AND_STRIPE_PAYOUT")

    def test_test_credit_candidates_are_archived_not_presented_as_revenue(self):
        self.assertEqual(PRIORITIES["agentmarket"].tier, "ARCHIVE")
        self.assertEqual(PRIORITIES["chowdr"].tier, "ARCHIVE")
        self.assertEqual(PRIORITIES["agentmarket"].autonomous_earning_ceiling_score, 0)
        self.assertEqual(PRIORITIES["chowdr"].autonomous_earning_ceiling_score, 0)
        self.assertNotIn("agentmarket", ACTIVE_MARKETS)
        self.assertNotIn("chowdr", ACTIVE_MARKETS)

    def test_market_dashboard_source_filters_to_active_catalog_and_preserves_history(self):
        source = Path("control/services/market_priority_dashboard.py").read_text(encoding="utf-8")
        self.assertIn("filter(slug__in=ACTIVE_MARKETS)", source)
        self.assertIn("filter(slug__in=INACTIVE_MARKETS).count()", source)
        self.assertIn("inactive_market_candidates", source)
        self.assertIn("exclude(slug__in=CANONICAL_EARNING_MARKETS).count()", source)
        self.assertIn("retired_non_earning_rows_hidden", source)
        self.assertNotIn("delete()", source)

    def test_taskbounty_uses_only_owner_configured_usd_bank_payout(self):
        catalog_source = Path("markets/catalog.py").read_text(encoding="utf-8")
        adapter_source = Path("markets/taskbounty/client.py").read_text(encoding="utf-8")
        self.assertIn("supported_payout_selected", catalog_source)
        self.assertIn("USD_BANK_TRANSFER", catalog_source)
        self.assertNotIn("/solver/payout-method", adapter_source)
        self.assertNotIn("PAYOUT_METHODS", adapter_source)
        self.assertIn("payout api is disabled", adapter_source.lower())
        self.assertNotIn("seed_phrase", adapter_source)

    def test_owner_market_ui_explains_priority_and_cleanup(self):
        script = Path("control/static/control/markets.js").read_text(encoding="utf-8")
        self.assertIn("Payout autonomy", script)
        self.assertIn("SA setup", script)
        self.assertIn("Earning ceiling", script)
        self.assertIn("retired/non-earning database rows hidden", script)
        self.assertIn("Historical records are preserved", script)
        self.assertNotIn("28 markets", script)


if __name__ == "__main__":
    unittest.main()
