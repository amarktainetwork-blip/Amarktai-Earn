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
    PRIORITIES,
)


class MarketPriorityStdlibTests(unittest.TestCase):
    def test_priority_covers_exact_canonical_earning_catalog(self):
        self.assertEqual(set(PRIORITIES), set(CANONICAL_EARNING_MARKETS))
        self.assertEqual(len(CANONICAL_EARNING_MARKETS), 27)
        self.assertEqual(len(ACTIVE_MARKETS), 25)
        self.assertEqual(ARCHIVED_MARKETS, {"agentmarket", "chowdr"})
        self.assertNotIn("internal-genx-proof", CANONICAL_EARNING_MARKETS)

    def test_activation_order_starts_with_best_autonomous_sa_routes(self):
        ordered = sorted(PRIORITIES.items(), key=lambda item: item[1].rank)
        self.assertEqual(
            [slug for slug, _priority in ordered[:4]],
            ["lemon-squeezy", "rapidapi", "apify-store", "taskbounty"],
        )
        self.assertTrue(all(priority.tier == "ACTIVATE_FIRST" for _slug, priority in ordered[:4]))
        self.assertEqual([priority.rank for _slug, priority in ordered], list(range(1, 28)))

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
        self.assertIn("exclude(slug__in=CANONICAL_EARNING_MARKETS).count()", source)
        self.assertIn("retired_non_earning_rows_hidden", source)
        self.assertNotIn("delete()", source)

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
