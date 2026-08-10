import unittest

from control.channel_launch_rules import PRIORITY_CHANNEL_SPECS, build_channel_launch_plan


class PriorityChannelLaunchRuleTests(unittest.TestCase):
    def _profile(self, slug):
        spec = PRIORITY_CHANNEL_SPECS[slug]
        return {
            "hosting_policy": "WEBDOCK_SAFE",
            "blockers": ["ACCOUNT_NOT_CONFIGURED"],
            "catalog_truth": {"execution_placement": spec.execution_placement},
        }

    def test_priority_channels_prepare_without_external_mutation(self):
        for slug in PRIORITY_CHANNEL_SPECS:
            with self.subTest(slug=slug):
                plan = build_channel_launch_plan(
                    slug=slug,
                    market={"enabled": False, "status": "PAYOUT_BLOCKED", "payout_ready": False, "south_africa_verified": False},
                    profile=self._profile(slug),
                    route={"ready": False, "blockers": ["SETTLEMENT_ROUTE_NOT_VERIFIED"]},
                )
                self.assertTrue(plan["shadow_preparation_ready"])
                self.assertFalse(plan["activation_ready"])
                self.assertFalse(plan["external_mutation_allowed"])
                self.assertIn("ACCOUNT_NOT_CONFIGURED", plan["external_blockers"])
                self.assertIn("VERIFIED_OWNER_SETTLEMENT_ROUTE_REQUIRED", plan["external_blockers"])
                self.assertIn("MARKET_DISABLED", plan["external_blockers"])
                self.assertIn("MARKET_NOT_LIVE", plan["external_blockers"])
                self.assertTrue(plan["actions"])

    def test_disabled_or_non_live_market_cannot_be_activation_ready(self):
        for market in (
            {"enabled": False, "status": "LIVE", "payout_ready": True, "south_africa_verified": True},
            {"enabled": True, "status": "PAYOUT_BLOCKED", "payout_ready": True, "south_africa_verified": True},
        ):
            with self.subTest(market=market):
                plan = build_channel_launch_plan(
                    slug="rapidapi",
                    market=market,
                    profile={
                        "hosting_policy": "WEBDOCK_SAFE",
                        "blockers": [],
                        "catalog_truth": {"execution_placement": "WEBDOCK_LIGHT"},
                    },
                    route={"ready": True, "blockers": []},
                )
                self.assertTrue(plan["shadow_preparation_ready"])
                self.assertFalse(plan["activation_ready"])
        enabled_blockers = build_channel_launch_plan(
            slug="rapidapi",
            market={"enabled": False, "status": "LIVE", "payout_ready": True, "south_africa_verified": True},
            profile={"hosting_policy": "WEBDOCK_SAFE", "blockers": [], "catalog_truth": {"execution_placement": "WEBDOCK_LIGHT"}},
            route={"ready": True, "blockers": []},
        )["external_blockers"]
        self.assertIn("MARKET_DISABLED", enabled_blockers)

    def test_fully_ready_market_can_be_activation_ready(self):
        plan = build_channel_launch_plan(
            slug="rapidapi",
            market={"enabled": True, "status": "LIVE", "payout_ready": True, "south_africa_verified": True},
            profile={
                "hosting_policy": "WEBDOCK_SAFE",
                "blockers": [],
                "catalog_truth": {"execution_placement": "WEBDOCK_LIGHT"},
            },
            route={"ready": True, "blockers": []},
        )
        self.assertTrue(plan["shadow_preparation_ready"])
        self.assertTrue(plan["activation_ready"])
        self.assertFalse(plan["external_mutation_allowed"])

    def test_wrong_execution_placement_blocks_internal_preparation(self):
        plan = build_channel_launch_plan(
            slug="apify-store",
            market={"enabled": False, "status": "PAYOUT_BLOCKED", "payout_ready": False, "south_africa_verified": False},
            profile={
                "hosting_policy": "WEBDOCK_SAFE",
                "blockers": [],
                "catalog_truth": {"execution_placement": "WEBDOCK_LIGHT"},
            },
            route={"ready": False, "blockers": []},
        )
        self.assertFalse(plan["shadow_preparation_ready"])
        self.assertIn("EXECUTION_PLACEMENT_NOT_PROVEN", plan["internal_blockers"])

    def test_offhost_policy_never_becomes_webdock_preparation_ready(self):
        plan = build_channel_launch_plan(
            slug="contra",
            market={"enabled": False, "status": "PAYOUT_BLOCKED", "payout_ready": False, "south_africa_verified": False},
            profile={
                "hosting_policy": "OFFHOST_SETTLEMENT_REQUIRED",
                "blockers": [],
                "catalog_truth": {"execution_placement": "WEBDOCK_LIGHT"},
            },
            route={"ready": False, "blockers": []},
        )
        self.assertFalse(plan["shadow_preparation_ready"])
        self.assertIn("WEBDOCK_EXECUTION_NOT_ALLOWED", plan["internal_blockers"])


if __name__ == "__main__":
    unittest.main()
