import os
import unittest
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from control.services.revenue_portfolio import PortfolioCandidate, idle_capacity_actions, rank_portfolio_candidates
from control.services.seller_protocols import (
    DisabledExternalSettlementBridge,
    NeverminedFiatContract,
    SellerProtocolError,
    SkyfireSellerContract,
)
from markets.revenue_catalog import HOSTING_POLICIES, REVENUE_CHANNELS


class TwoSidedRevenueDeterministicTests(unittest.TestCase):
    def test_revenue_channel_and_hosting_taxonomy_is_explicit(self):
        self.assertEqual(set(REVENUE_CHANNELS), {
            "POSTED_JOB", "BOUNTY", "SERVICE_LISTING", "PAY_PER_CALL_API",
            "PROJECT_HIRE", "SUBSCRIPTION", "MANUAL_STOREFRONT", "OFFHOST_SETTLEMENT",
        })
        self.assertEqual(set(HOSTING_POLICIES), {"WEBDOCK_SAFE", "OFFHOST_SETTLEMENT_REQUIRED", "UNVERIFIED"})

    def test_nevermined_webdock_rejects_crypto(self):
        contract = NeverminedFiatContract()
        with self.assertRaisesRegex(SellerProtocolError, "NEVERMINED_WEBDOCK_FIAT_ONLY"):
            contract.prepare(
                name="fixture", interface="MCP", endpoint="https://example.test/mcp",
                price=Decimal("2"), currency="USDC", price_type="FIXED_PRICE",
            )
        result = contract.prepare(
            name="fixture", interface="A2A", endpoint="https://example.test/agent-card.json",
            price=Decimal("2"), currency="USD", price_type="FIXED_FIAT_PRICE",
        )
        self.assertFalse(result.mutation_performed)
        self.assertEqual(result.pricing["currency"], "USD")

    def test_skyfire_rejects_coin_settlement(self):
        contract = SkyfireSellerContract()
        with self.assertRaisesRegex(SellerProtocolError, "SKYFIRE_JWKS_SIGNATURE_NOT_VERIFIED"):
            contract.validate_claims(
                {"ssi": "service-1", "stp": "BANK", "cur": "USD", "amount": "1.00"},
                expected_service_id="service-1", signature_verified=False,
            )
        with self.assertRaisesRegex(SellerProtocolError, "SKYFIRE_NON_CRYPTO_SETTLEMENT_REQUIRED"):
            contract.validate_claims(
                {"ssi": "service-1", "stp": "COIN", "cur": "USD", "amount": "1.00"},
                expected_service_id="service-1", signature_verified=True,
            )
        result = contract.validate_claims(
            {"ssi": "service-1", "stp": "BANK", "cur": "USD", "amount": "1.00"},
            expected_service_id="service-1", signature_verified=True,
        )
        self.assertFalse(result["charge_performed"])

    def test_offhost_bridge_is_deliberately_unimplemented(self):
        with self.assertRaisesRegex(SellerProtocolError, "EXTERNAL_SETTLEMENT_BRIDGE_NOT_IMPLEMENTED"):
            DisabledExternalSettlementBridge().submit_settlement_evidence(market="fixture", reference="r1", payload={})
        env_text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8").casefold()
        self.assertNotIn("private_key", env_text)
        self.assertNotIn("wallet_secret", env_text)

    def test_profit_per_minute_can_outrank_larger_job(self):
        larger = PortfolioCandidate(
            job_id="large", source_type="POSTED_OPPORTUNITY", revenue_channel="POSTED_JOB",
            expected_net_profit=Decimal("100"), risk_adjusted_profit=Decimal("80"), productive_minutes=Decimal("400"),
            payout_probability=Decimal("0.90"), acceptance_probability=Decimal("0.90"),
        )
        smaller = PortfolioCandidate(
            job_id="small", source_type="INBOUND_SERVICE_ORDER", revenue_channel="SERVICE_LISTING",
            expected_net_profit=Decimal("30"), risk_adjusted_profit=Decimal("28"), productive_minutes=Decimal("20"),
            payout_probability=Decimal("0.95"), acceptance_probability=Decimal("0.95"),
        )
        ranked = rank_portfolio_candidates([larger, smaller], available_slots=2, productive_minutes_available=Decimal("500"))
        self.assertEqual([row.candidate.job_id for row in ranked], ["small", "large"])

    def test_portfolio_ranks_channels_globally_and_never_uses_targets_as_caps(self):
        candidates = [
            PortfolioCandidate(
                job_id=f"job-{index}", source_type=source, revenue_channel=channel,
                expected_net_profit=Decimal(profit), risk_adjusted_profit=Decimal(profit), productive_minutes=Decimal(minutes),
                payout_probability=Decimal("0.9"), acceptance_probability=Decimal("0.9"),
            )
            for index, (source, channel, profit, minutes) in enumerate((
                ("POSTED_OPPORTUNITY", "POSTED_JOB", "20", "20"),
                ("BOUNTY", "BOUNTY", "30", "60"),
                ("INBOUND_SERVICE_ORDER", "SERVICE_LISTING", "25", "10"),
            ))
        ]
        ranked = rank_portfolio_candidates(candidates, available_slots=2, productive_minutes_available=Decimal("30"))
        self.assertEqual(ranked[0].candidate.revenue_channel, "SERVICE_LISTING")
        self.assertEqual(sum(row.selected for row in ranked), 2)

    def test_idle_actions_do_not_invent_paid_work(self):
        actions = idle_capacity_actions(enabled_market_slugs=["fixture"], offerings=[])
        self.assertTrue(all(action["paid_execution"] is False for action in actions))
        self.assertFalse(any(action.get("auto_publish") for action in actions))
        self.assertFalse(any(action.get("auto_accept") for action in actions))


if __name__ == "__main__":
    unittest.main()
