import os
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from control.economics import EconomicsInput
from control.models import (
    AcquisitionPreflight,
    Execution,
    GenXAccountSnapshot,
    InboundOrder,
    Job,
    LedgerEntry,
    MarketIntegrationProfile,
    MarketServiceListing,
    Marketplace,
    Payout,
    PortfolioDecision,
    QAResult,
    ServiceOffering,
    TreasuryBalance,
    Worker,
)
from control.ops import markets_snapshot, overview_snapshot
from control.services.jobs import score_and_persist, transition_job
from control.services.profit_brain import UtilizationState, settled_profit_truth
from control.services.revenue_portfolio import persist_portfolio_ranking
from control.services.seller_services import (
    is_offering_currently_sellable,
    listing_blockers,
    pause_service_listing,
    receive_inbound_order,
    recommend_offering_price,
    record_inbound_delivery,
    record_inbound_service_message,
    record_inbound_usage,
    record_listing_publication,
    reconcile_inbound_settlement,
    refresh_listing_truth,
    refresh_service_offering_proof,
    service_capability_blockers,
    sync_candidate_service_offerings,
    version_service_offering,
)
from control.services.markets import bootstrap_market_integrations
from markets.revenue_catalog import bootstrap_revenue_market_catalog


class TwoSidedRevenueIntegrationTests(TestCase):
    maxDiff = None

    def setUp(self):
        bootstrap_market_integrations()
        bootstrap_revenue_market_catalog()
        self.nevermined = Marketplace.objects.get(slug="nevermined")

    def _offering(self, *, operation="json_to_csv", worker_class="structured_data", cost="0.10", slug=None):
        return ServiceOffering.objects.create(
            slug=slug or f"fixture-{operation.replace('_', '-')}",
            display_name="Fixture service",
            description="Deterministic fixture",
            capability=worker_class,
            operation=operation,
            worker_class=worker_class,
            pricing_model=ServiceOffering.PricingModel.FIXED_PROJECT,
            currency="USD",
            advertised_price="10.00",
            minimum_profitable_price="1.00",
            platform_fee_rate="0.10",
            expected_operational_cost=cost,
            expected_minutes=10,
            enabled=True,
            accepting_orders=True,
            proof_state=ServiceOffering.ProofState.SOURCE_PROVEN,
        )

    def _record_execution_proof(self, offering):
        proof_market, _ = Marketplace.objects.get_or_create(slug="proof-market", defaults={"display_name": "Proof"})
        proof_job = Job.objects.create(
            marketplace=proof_market, external_id=f"proof-{offering.slug}", title="Proof",
            task_class=offering.capability, reward="1", normalized_payload={"operation": offering.operation},
        )
        worker = Worker.objects.create(
            id=f"worker-{offering.slug}"[:120], worker_class=offering.worker_class, version="1.0.0", status="READY",
        )
        execution = Execution.objects.create(
            job=proof_job, worker=worker, status="COMPLETED", result={"operation": offering.operation},
        )
        QAResult.objects.create(job=proof_job, execution=execution, check_type="independent", passed=True, score="1")
        return offering

    def _prove(self, offering):
        self._record_execution_proof(offering)
        refresh_service_offering_proof(offering)
        offering.refresh_from_db()
        self.assertEqual(offering.proof_state, ServiceOffering.ProofState.EXECUTION_PROVEN)
        refresh_service_offering_proof(offering)
        offering.refresh_from_db()
        self.assertEqual(offering.proof_state, ServiceOffering.ProofState.SELLABLE)
        return offering

    def _ready_market(self, market=None):
        market = market or self.nevermined
        market.enabled = True
        market.status = Marketplace.Status.LIVE
        market.payout_ready = True
        market.south_africa_verified = True
        market.fee_rate = Decimal("0.10")
        market.save()
        profile = market.integration_profile
        profile.policy_verified = True
        profile.hosting_policy = "WEBDOCK_SAFE"
        profile.blockers = []
        profile.seller_capabilities = {**profile.seller_capabilities, "publish_service": True, "receive_orders": True}
        profile.save()
        policy = market.policy_versions.order_by("-checked_at").first()
        policy.automation_allowed = True
        policy.webdock_compatible = True
        policy.checked_at = timezone.now()
        policy.save()
        return market

    def _listing(self, offering, market=None):
        market = market or self.nevermined
        return MarketServiceListing.objects.create(
            offering=offering,
            marketplace=market,
            published_price=offering.advertised_price,
            currency="USD",
            pricing_model=offering.pricing_model,
            status=MarketServiceListing.Status.PUBLISHED,
            platform_metadata={"price_type": "FIXED_FIAT_PRICE", "settlement_type": "BANK"},
        )

    def _receive(self, listing, *, remote="order-1", key="key-1", price="20", fee="2", funding="ESCROW"):
        return receive_inbound_order(
            marketplace=listing.marketplace,
            listing=listing,
            remote_order_id=remote,
            idempotency_key=key,
            payload={
                "requirements": {"source": "fixture"},
                "input_assets": [],
                "quoted_price": price,
                "platform_fee": fee,
                "currency": "USD",
                "funding_state": funding,
            },
            authenticated_market_identity=True,
            authenticated_at=timezone.now(),
        )

    def test_existing_six_markets_remain_fail_closed(self):
        slugs = ("agentgigs", "dealwork", "callboard", "taskbounty", "opire", "algora")
        before = {
            slug: (market.enabled, market.payout_ready, market.south_africa_verified, market.integration_profile.autonomous_acquisition_enabled)
            for slug in slugs for market in [Marketplace.objects.get(slug=slug)]
        }
        bootstrap_revenue_market_catalog()
        after = {
            slug: (market.enabled, market.payout_ready, market.south_africa_verified, market.integration_profile.autonomous_acquisition_enabled)
            for slug in slugs for market in [Marketplace.objects.get(slug=slug)]
        }
        self.assertEqual(before, after)
        self.assertTrue(all(values == (False, False, False, False) for values in after.values()))

    def test_existing_six_profiles_receive_static_taxonomy_without_dynamic_truth_reset(self):
        expected_channels = {
            "agentgigs": ["POSTED_JOB"], "dealwork": ["POSTED_JOB"], "callboard": ["POSTED_JOB"],
            "taskbounty": ["BOUNTY"], "opire": ["BOUNTY"], "algora": ["BOUNTY"],
        }
        profiles = MarketIntegrationProfile.objects.filter(marketplace__slug__in=expected_channels)
        profiles.update(
            revenue_channels=[], seller_capabilities={}, hosting_policy="UNVERIFIED",
            api_contract_state="UNVERIFIED", job_acquisition_mode="", seller_mode="", settlement_rail="",
        )
        agentgigs = Marketplace.objects.get(slug="agentgigs").integration_profile
        agentgigs.payout_proof_state = "OPERATOR_EVIDENCE_PRESERVED"
        agentgigs.automation_status = "OPERATOR_MANAGED_STATUS"
        agentgigs.blockers = ["OPERATOR_MANAGED_BLOCKER"]
        agentgigs.evidence = {"operator": {"identity": "preserve-me"}}
        agentgigs.save()

        first = bootstrap_revenue_market_catalog()
        second = bootstrap_revenue_market_catalog()
        self.assertGreaterEqual(first["updated"], 6)
        self.assertEqual(second["updated"], 0)
        for slug, channels in expected_channels.items():
            market = Marketplace.objects.get(slug=slug)
            profile = market.integration_profile
            self.assertEqual(profile.revenue_channels, channels)
            self.assertEqual(profile.hosting_policy, "WEBDOCK_SAFE")
            self.assertTrue(profile.api_contract_state != "UNVERIFIED")
            self.assertTrue(profile.seller_capabilities)
            self.assertFalse(any(profile.seller_capabilities.values()))
            self.assertFalse(market.enabled)
            self.assertFalse(market.payout_ready)
            self.assertFalse(market.south_africa_verified)
            self.assertFalse(profile.autonomous_acquisition_enabled)
        agentgigs.refresh_from_db()
        self.assertEqual(agentgigs.payout_proof_state, "OPERATOR_EVIDENCE_PRESERVED")
        self.assertEqual(agentgigs.automation_status, "OPERATOR_MANAGED_STATUS")
        self.assertEqual(agentgigs.blockers, ["OPERATOR_MANAGED_BLOCKER"])
        self.assertEqual(agentgigs.evidence, {"operator": {"identity": "preserve-me"}})
        self.assertNotIn("SERVICE_LISTING", Marketplace.objects.get(slug="dealwork").integration_profile.revenue_channels)

    def test_service_candidate_sync_reports_existing_rows_as_unchanged(self):
        first = sync_candidate_service_offerings()
        second = sync_candidate_service_offerings()
        self.assertGreater(first["created"], 0)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["unchanged"], second["total"])
        self.assertNotIn("updated", second)

    def test_nevermined_publish_is_blocked_without_external_proof(self):
        offering = self._offering()
        listing = self._listing(offering)
        refresh_listing_truth(listing)
        self.assertEqual(listing.status, MarketServiceListing.Status.BLOCKED)
        self.assertIn("ACCOUNT_NOT_CONFIGURED", listing.failure_detail)
        self.assertIn("STRIPE_CONNECT_NOT_VERIFIED", listing.failure_detail)
        self.assertIn("SOUTH_AFRICA_PAYOUT_NOT_VERIFIED", listing.failure_detail)

    def test_skyfire_service_requires_noncrypto_seller_and_payout_evidence(self):
        market = Marketplace.objects.get(slug="skyfire")
        listing = self._listing(self._offering(slug="skyfire-fixture"), market)
        listing.platform_metadata = {"settlement_type": "COIN"}
        listing.save()
        reasons = listing_blockers(listing)
        self.assertIn("SKYFIRE_NON_CRYPTO_SETTLEMENT_REQUIRED", reasons)
        self.assertIn("SELLER_KYA_NOT_VERIFIED", reasons)
        self.assertIn("NON_CRYPTO_SETTLEMENT_NOT_VERIFIED", reasons)

    def test_hyrve_has_no_invented_source_contract(self):
        profile = Marketplace.objects.get(slug="hyrve").integration_profile
        self.assertFalse(profile.source_wired)
        self.assertTrue(profile.manual_onboarding_required)
        self.assertEqual(profile.api_contract_state, "PUBLIC_API_CONTRACT_NOT_VERIFIED")

    def test_offhost_candidates_cannot_become_webdock_runtime_channels(self):
        self.assertFalse(Marketplace.objects.filter(slug="virtuals-acp").exists())

    def test_service_requires_real_execution_and_qa_proof(self):
        offering = self._offering()
        refresh_service_offering_proof(offering)
        offering.refresh_from_db()
        self.assertEqual(offering.proof_state, ServiceOffering.ProofState.SOURCE_PROVEN)
        self.assertIn("SERVICE_EXECUTION_NOT_PROVEN", service_capability_blockers(offering))
        self._prove(offering)
        self.assertNotIn("SERVICE_EXECUTION_NOT_PROVEN", service_capability_blockers(offering))

    def test_proof_progression_and_runtime_sellability_gates_owner_count(self):
        coding = self._offering(operation="code_change_small", worker_class="code_small", slug="coding-fixture")
        public_web = self._offering(operation="public_web_extract", worker_class="public_web_data", slug="web-fixture")
        self._record_execution_proof(coding)
        self._record_execution_proof(public_web)
        with patch.dict(os.environ, {"SANDBOX_CODING_ENABLED": "0", "PUBLIC_WEB_DATA_ENABLED": "0"}):
            refresh_service_offering_proof(coding)
            refresh_service_offering_proof(public_web)
            coding.refresh_from_db(); public_web.refresh_from_db()
            self.assertEqual(coding.proof_state, ServiceOffering.ProofState.EXECUTION_PROVEN)
            self.assertEqual(public_web.proof_state, ServiceOffering.ProofState.EXECUTION_PROVEN)
            refresh_service_offering_proof(coding)
            refresh_service_offering_proof(public_web)
            coding.refresh_from_db(); public_web.refresh_from_db()
            self.assertEqual(coding.proof_state, ServiceOffering.ProofState.EXECUTION_PROVEN)
            self.assertEqual(public_web.proof_state, ServiceOffering.ProofState.EXECUTION_PROVEN)
            self.assertIn("CODING_SERVICE_BLOCKED_SANDBOX_OFF", service_capability_blockers(coding))
            self.assertIn("PUBLIC_WEB_SERVICE_BLOCKED_WEB_DISABLED", service_capability_blockers(public_web))
            self.assertFalse(is_offering_currently_sellable(coding))
            self.assertFalse(is_offering_currently_sellable(public_web))
            self.assertEqual(markets_snapshot()["meta"]["sellable_offerings"], 0)
        with patch.dict(os.environ, {"SANDBOX_CODING_ENABLED": "1", "PUBLIC_WEB_DATA_ENABLED": "1"}):
            refresh_service_offering_proof(coding)
            refresh_service_offering_proof(public_web)
            coding.refresh_from_db(); public_web.refresh_from_db()
            self.assertEqual(coding.proof_state, ServiceOffering.ProofState.SELLABLE)
            self.assertEqual(public_web.proof_state, ServiceOffering.ProofState.SELLABLE)
            self.assertTrue(is_offering_currently_sellable(coding))
            self.assertTrue(is_offering_currently_sellable(public_web))
            self.assertEqual(markets_snapshot()["meta"]["sellable_offerings"], 2)

    def test_inbound_order_is_one_canonical_job_and_idempotent(self):
        market = self._ready_market()
        offering = self._prove(self._offering())
        listing = self._listing(offering, market)
        first, created = self._receive(listing)
        second, duplicate_created = self._receive(listing)
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(Job.objects.filter(marketplace=market, external_id="inbound:order-1").count(), 1)
        self.assertEqual(first.job.normalized_payload["source_type"], "INBOUND_SERVICE_ORDER")

    def test_inbound_order_requires_auth_replay_and_safe_asset_contract(self):
        market = self._ready_market()
        offering = self._prove(self._offering())
        listing = self._listing(offering, market)
        with self.assertRaisesRegex(ValueError, "INBOUND_MARKET_AUTHENTICATION_REQUIRED"):
            receive_inbound_order(
                marketplace=market, listing=listing, remote_order_id="bad", idempotency_key="bad",
                payload={"requirements": {}, "input_assets": [], "quoted_price": "10", "platform_fee": "1"},
                authenticated_market_identity=False, authenticated_at=timezone.now(),
            )
        with self.assertRaisesRegex(ValueError, "INBOUND_REPLAY_WINDOW_EXCEEDED"):
            receive_inbound_order(
                marketplace=market, listing=listing, remote_order_id="old", idempotency_key="old",
                payload={"requirements": {}, "input_assets": [], "quoted_price": "10", "platform_fee": "1"},
                authenticated_market_identity=True, authenticated_at=timezone.now() - timedelta(hours=1),
            )
        with self.assertRaisesRegex(ValueError, "INBOUND_UNSAFE_FILE_MATERIAL_REJECTED"):
            receive_inbound_order(
                marketplace=market, listing=listing, remote_order_id="file", idempotency_key="file",
                payload={"requirements": {}, "input_assets": [{"asset_id": "a", "sha256": "f" * 64, "path": "/tmp/x"}], "quoted_price": "10", "platform_fee": "1"},
                authenticated_market_identity=True, authenticated_at=timezone.now(),
            )

    def test_inbound_preflight_rejects_negative_profit_and_does_not_cap_high_profit(self):
        market = self._ready_market()
        offering = self._prove(self._offering(cost="5"))
        listing = self._listing(offering, market)
        low, _ = self._receive(listing, remote="low", key="low", price="4", fee="0")
        self.assertEqual(low.status, InboundOrder.Status.PREFLIGHT_BLOCKED)
        self.assertIn("EXPECTED_NET_PROFIT_NOT_POSITIVE", low.economic_preflight["reason_codes"])
        with patch.dict(os.environ, {"TARGET_DAILY_SETTLED_PROFIT": "0.01", "TARGET_WEEKLY_SETTLED_PROFIT": "0.01"}):
            high, _ = self._receive(listing, remote="high", key="high", price="1000", fee="100")
        self.assertEqual(high.status, InboundOrder.Status.READY)
        self.assertGreater(Decimal(high.economic_preflight["expected_net_profit"]), Decimal("800"))
        self.assertIn("INBOUND_SERVICE_AUTO_ACCEPT_DISABLED", high.economic_preflight["reason_codes"])
        self.assertFalse(high.economic_preflight["action_allowed"])

    def test_fee_is_counted_once_in_inbound_preflight(self):
        market = self._ready_market()
        offering = self._prove(self._offering(cost="5"))
        order, _ = self._receive(self._listing(offering, market), price="100", fee="10")
        preflight = order.job.acquisition_preflights.latest("created_at")
        self.assertEqual(preflight.marketplace_fee, Decimal("10"))
        self.assertEqual(preflight.expected_net, Decimal("85"))
        self.assertEqual(preflight.details["fee_semantics"], "PLATFORM_FEE_COUNTED_ONCE")

    def test_inbound_preflight_enforces_available_genx_credit_budget(self):
        market = self._ready_market()
        offering = self._prove(self._offering(cost="0.10"))
        offering.expected_genx_cost = Decimal("0.25")
        offering.max_genx_credits = Decimal("5")
        offering.save()
        listing = self._listing(offering, market)
        missing, _ = self._receive(listing, remote="credits-missing", key="credits-missing", price="50", fee="5")
        self.assertIn("GENX_BALANCE_UNVERIFIED", missing.economic_preflight["reason_codes"])
        GenXAccountSnapshot.objects.create(available_credits=Decimal("4"), raw={"fixture": True})
        low, _ = self._receive(listing, remote="credits-low", key="credits-low", price="50", fee="5")
        self.assertIn("GENX_BUDGET_INSUFFICIENT", low.economic_preflight["reason_codes"])
        GenXAccountSnapshot.objects.create(available_credits=Decimal("5"), raw={"fixture": True})
        ready, _ = self._receive(listing, remote="credits-ready", key="credits-ready", price="50", fee="5")
        self.assertEqual(ready.status, InboundOrder.Status.READY)

    def test_pricing_is_bounded_by_profit_floor(self):
        offering = self._offering(cost="4")
        offering.minimum_profitable_price = Decimal("7")
        offering.advertised_price = Decimal("10")
        offering.save()
        idle = recommend_offering_price(offering, utilization_state=UtilizationState.IDLE, historical_win_rate=Decimal("0.10"))
        busy = recommend_offering_price(offering, utilization_state=UtilizationState.BUSY, historical_win_rate=Decimal("0.90"))
        self.assertGreaterEqual(idle.price, Decimal("7"))
        self.assertGreater(busy.price, idle.price)
        self.assertLessEqual(abs(busy.adjustment_fraction), Decimal("0.20"))

    def test_global_portfolio_ranking_persists_cross_source_decisions(self):
        market = self._ready_market()
        posted = Job.objects.create(
            marketplace=market, external_id="posted-rank", title="Large posted job", task_class="analysis", reward="100",
            normalized_payload={"source_type": "POSTED_OPPORTUNITY", "revenue_channel": "POSTED_JOB"},
        )
        inbound = Job.objects.create(
            marketplace=market, external_id="inbound-rank", title="Efficient inbound job", task_class="analysis", reward="30",
            normalized_payload={"source_type": "INBOUND_SERVICE_ORDER", "revenue_channel": "SERVICE_LISTING"},
        )
        score_and_persist(posted, EconomicsInput(
            gross_reward=Decimal("100"), marketplace_fee=Decimal("10"), p_acquire=Decimal("1"),
            p_accept=Decimal("0.9"), p_payment=Decimal("0.9"), estimated_worker_minutes=Decimal("200"),
        ))
        score_and_persist(inbound, EconomicsInput(
            gross_reward=Decimal("30"), marketplace_fee=Decimal("3"), p_acquire=Decimal("1"),
            p_accept=Decimal("0.95"), p_payment=Decimal("0.95"), estimated_worker_minutes=Decimal("10"),
        ))
        ranked = persist_portfolio_ranking(
            [posted, inbound], available_slots=2, productive_minutes_available=Decimal("300"),
        )
        self.assertEqual(ranked[0].candidate.job_id, str(inbound.id))
        self.assertFalse(any(row.selected for row in ranked))
        self.assertTrue(all("NO_VALID_ACQUISITION_PREFLIGHT" in row.candidate.selection_blockers for row in ranked))
        self.assertEqual(set(PortfolioDecision.objects.values_list("source_type", flat=True)), {"POSTED_OPPORTUNITY", "INBOUND_SERVICE_ORDER"})

    def test_portfolio_ranks_blocked_work_but_selects_only_currently_safe_actions(self):
        safe_market = self._ready_market()
        safe_profile = safe_market.integration_profile
        safe_profile.autonomous_acquisition_enabled = True
        safe_profile.save()

        payout_blocked_market = self._ready_market(Marketplace.objects.get(slug="skyfire"))
        payout_blocked_market.payout_ready = False
        payout_blocked_market.save()
        payout_blocked_profile = payout_blocked_market.integration_profile
        payout_blocked_profile.autonomous_acquisition_enabled = True
        payout_blocked_profile.save()

        stale_market = self._ready_market(Marketplace.objects.get(slug="callboard"))
        stale_profile = stale_market.integration_profile
        stale_profile.autonomous_acquisition_enabled = True
        stale_profile.save()
        stale_policy = stale_market.policy_versions.order_by("-checked_at").first()
        stale_policy.checked_at = timezone.now() - timedelta(days=31)
        stale_policy.save()

        def scored_job(market, external_id, reward, minutes):
            job = Job.objects.create(
                marketplace=market, external_id=external_id, title=external_id, task_class="analysis", reward=reward,
                normalized_payload={"source_type": "POSTED_OPPORTUNITY", "revenue_channel": "POSTED_JOB"},
            )
            score_and_persist(job, EconomicsInput(
                gross_reward=Decimal(reward), marketplace_fee=Decimal("0"), p_acquire=Decimal("1"), p_accept=Decimal("0.95"),
                p_payment=Decimal("0.95"), estimated_worker_minutes=Decimal(minutes),
            ))
            return job

        payout_blocked = scored_job(payout_blocked_market, "payout-blocked-rank", "1000", "10")
        AcquisitionPreflight.objects.create(
            job=payout_blocked, autonomy_mode="LOW_RISK", eligible=True, allowed=True,
        )
        no_preflight = scored_job(safe_market, "no-preflight-rank", "500", "10")
        stale = scored_job(stale_market, "stale-policy-rank", "250", "10")
        AcquisitionPreflight.objects.create(job=stale, autonomy_mode="LOW_RISK", eligible=True, allowed=True)
        safe = scored_job(safe_market, "safe-rank", "50", "10")
        AcquisitionPreflight.objects.create(job=safe, autonomy_mode="LOW_RISK", eligible=True, allowed=True)

        offering = self._prove(self._offering(slug="inbound-selection-fixture"))
        inbound_order, _ = self._receive(
            self._listing(offering, safe_market), remote="selection-inbound", key="selection-inbound", price="40", fee="2",
        )
        self.assertTrue(inbound_order.economic_preflight["eligible"])
        self.assertFalse(inbound_order.economic_preflight["action_allowed"])

        env = {
            "AUTONOMOUS_MODE": "LOW_RISK",
            "NEVERMINED_AUTO_ACQUIRE_ENABLED": "1",
            "SKYFIRE_AUTO_ACQUIRE_ENABLED": "1",
            "CALLBOARD_AUTO_ACQUIRE_ENABLED": "1",
            "INBOUND_SERVICE_AUTO_ACCEPT_ENABLED": "0",
        }
        with patch.dict(os.environ, env):
            ranked = persist_portfolio_ranking(
                [payout_blocked, no_preflight, stale, safe, inbound_order.job],
                available_slots=3,
                productive_minutes_available=Decimal("100"),
            )
        by_job = {row.candidate.job_id: row for row in ranked}
        self.assertEqual(ranked[0].candidate.job_id, str(payout_blocked.id))
        self.assertFalse(by_job[str(payout_blocked.id)].selected)
        self.assertIn("PAYOUT_NOT_READY", by_job[str(payout_blocked.id)].candidate.selection_blockers)
        self.assertFalse(by_job[str(no_preflight.id)].selected)
        self.assertIn("NO_VALID_ACQUISITION_PREFLIGHT", by_job[str(no_preflight.id)].candidate.selection_blockers)
        self.assertFalse(by_job[str(stale.id)].selected)
        self.assertIn("MARKET_AUTOMATION_POLICY_STALE", by_job[str(stale.id)].candidate.selection_blockers)
        self.assertFalse(by_job[str(inbound_order.job_id)].selected)
        self.assertTrue(by_job[str(inbound_order.job_id)].would_select_if_enabled)
        self.assertIn("INBOUND_SERVICE_AUTO_ACCEPT_DISABLED", by_job[str(inbound_order.job_id)].candidate.selection_blockers)
        self.assertTrue(by_job[str(safe.id)].selected)

    def test_stale_policy_blocks_listing(self):
        market = self._ready_market()
        offering = self._prove(self._offering())
        listing = self._listing(offering, market)
        policy = market.policy_versions.order_by("-checked_at").first()
        policy.checked_at = timezone.now() - timedelta(days=31)
        policy.save()
        env = {"NEVERMINED_API_KEY": "fixture", "SERVICE_AUTO_PUBLISH_ENABLED": "1", "NEVERMINED_AUTO_PUBLISH_ENABLED": "1"}
        with patch.dict(os.environ, env):
            self.assertIn("MARKET_POLICY_STALE", listing_blockers(listing))

    def test_common_seller_lifecycle_versions_pauses_messages_usage_and_delivery(self):
        market = self._ready_market()
        offering = self._prove(self._offering())
        original_version = offering.version
        offering = version_service_offering(offering, changes={"description": "Version two"})
        self.assertEqual(offering.version, original_version + 1)
        listing = self._listing(offering, market)
        listing.status = MarketServiceListing.Status.READY
        listing.save()
        listing = record_listing_publication(
            listing,
            remote_listing_id="listing-1",
            remote_reference="https://example.test/listing/1",
            remote_version="2",
            authoritative_evidence={"fixture": True},
        )
        self.assertEqual(listing.status, MarketServiceListing.Status.PUBLISHED)
        order, _ = self._receive(listing)
        self.assertTrue(record_inbound_service_message(order, remote_id="message-1", content="requirements", actor="market:fixture"))
        self.assertFalse(record_inbound_service_message(order, remote_id="message-1", content="requirements", actor="market:fixture"))
        self.assertTrue(record_inbound_usage(order, remote_event_id="usage-1", units=Decimal("3"), unit_type="request", authoritative_evidence={"fixture": True}))
        self.assertFalse(record_inbound_usage(order, remote_event_id="usage-1", units=Decimal("3"), unit_type="request", authoritative_evidence={"fixture": True}))
        with self.assertRaisesRegex(ValueError, "INBOUND_USAGE_IDEMPOTENCY_CONFLICT"):
            record_inbound_usage(order, remote_event_id="usage-1", units=Decimal("4"), unit_type="request", authoritative_evidence={"fixture": True})
        for state in (Job.State.AWARDED, Job.State.EXECUTING, Job.State.SUBMITTED):
            transition_job(order.job_id, state, actor="test")
        record_inbound_delivery(order, remote_reference="delivery-1", actor="market:fixture")
        order.refresh_from_db()
        self.assertEqual(order.status, InboundOrder.Status.DELIVERED)
        self.assertEqual(order.usage["total_units"], "3")
        listing = pause_service_listing(listing, reason="owner pause")
        self.assertEqual(listing.status, MarketServiceListing.Status.PAUSED)

    def test_authoritative_settlement_uses_canonical_finance_and_reversal_truth(self):
        market = self._ready_market()
        offering = self._prove(self._offering())
        order, _ = self._receive(self._listing(offering, market), price="100", fee="10")
        for state in (Job.State.AWARDED, Job.State.EXECUTING, Job.State.SUBMITTED, Job.State.ACCEPTED):
            transition_job(order.job_id, state, actor="test")
        reconcile_inbound_settlement(
            order, remote_event_id="authorized", state="AUTHORIZED", gross=Decimal("100"), fee=Decimal("10"),
            currency="USD", authoritative=False, evidence_source="fixture", evidence={"authorization": True},
        )
        reconcile_inbound_settlement(
            order, remote_event_id="escrow", state="ESCROW", gross=Decimal("100"), fee=Decimal("10"),
            currency="USD", authoritative=True, evidence_source="fixture", evidence={"escrow": True},
        )
        self.assertFalse(Payout.objects.filter(job=order.job).exists())
        pending_event, pending_created = reconcile_inbound_settlement(
            order, remote_event_id="pending", state="PAYOUT_PENDING", gross=Decimal("100"), fee=Decimal("10"),
            currency="USD", authoritative=True, evidence_source="fixture", evidence={"payout": "pending"},
        )
        self.assertTrue(pending_created)
        payout = Payout.objects.get(job=order.job)
        self.assertEqual(payout.state, Payout.State.PAYOUT_PENDING)
        self.assertTrue(LedgerEntry.objects.filter(entry_key=f"payout:{payout.id}:earned", event_type="PAYOUT_EARNED").exists())
        treasury = TreasuryBalance.objects.get(account=market.slug, currency="USD")
        self.assertEqual(treasury.pending, Decimal("90"))
        self.assertEqual(treasury.settled, Decimal("0"))
        reporting_start = timezone.now() - timedelta(days=1)
        self.assertEqual(settled_profit_truth(start=reporting_start).settled_cash, Decimal("0"))
        settled_event, settled_created = reconcile_inbound_settlement(
            order, remote_event_id="bank-confirmed", state="SETTLED", gross=Decimal("100"), fee=Decimal("10"),
            currency="USD", authoritative=True, evidence_source="bank-reconciliation", evidence={"irreversible": True},
        )
        self.assertTrue(settled_created)
        payout = Payout.objects.get(job=order.job)
        order.refresh_from_db(); order.job.refresh_from_db()
        self.assertEqual(payout.state, Payout.State.SETTLED)
        self.assertEqual(order.status, InboundOrder.Status.SETTLED)
        self.assertEqual(order.job.state, Job.State.SETTLED)
        self.assertTrue(LedgerEntry.objects.filter(entry_key=f"payout:{payout.id}:settled", event_type="PAYOUT_SETTLED").exists())
        treasury.refresh_from_db()
        self.assertEqual(treasury.pending, Decimal("0"))
        self.assertEqual(treasury.settled, Decimal("90"))
        self.assertEqual(settled_profit_truth(start=reporting_start).settled_cash, Decimal("90"))

        duplicate, duplicate_created = reconcile_inbound_settlement(
            order, remote_event_id="bank-confirmed", state="SETTLED", gross=Decimal("100"), fee=Decimal("10"),
            currency="USD", authoritative=True, evidence_source="bank-reconciliation", evidence={"irreversible": True},
        )
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.id, settled_event.id)
        with self.assertRaisesRegex(ValueError, "INBOUND_SETTLEMENT_IDEMPOTENCY_CONFLICT"):
            reconcile_inbound_settlement(
                order, remote_event_id="bank-confirmed", state="REVERSED", gross=Decimal("100"), fee=Decimal("10"),
                currency="USD", authoritative=True, evidence_source="bank-reconciliation", evidence={"reversal": True},
            )
        with self.assertRaisesRegex(ValueError, "payout amount mutation requires an explicit adjustment workflow"):
            reconcile_inbound_settlement(
                order, remote_event_id="adjusted-without-workflow", state="SETTLED", gross=Decimal("80"), fee=Decimal("5"),
                currency="USD", authoritative=True, evidence_source="bank-reconciliation", evidence={"irreversible": True},
            )
        self.assertFalse(order.settlement_events.filter(remote_event_id="adjusted-without-workflow").exists())

        reversal, reversal_created = reconcile_inbound_settlement(
            order, remote_event_id="bank-reversal", state="REVERSED", gross=Decimal("100"), fee=Decimal("10"),
            currency="USD", authoritative=True, evidence_source="bank-reconciliation", evidence={"reversal": True},
        )
        self.assertTrue(reversal_created)
        payout.refresh_from_db(); order.refresh_from_db(); treasury.refresh_from_db()
        self.assertEqual(payout.state, Payout.State.REVERSED)
        self.assertEqual(order.status, InboundOrder.Status.REVERSED)
        self.assertEqual(treasury.settled, Decimal("0"))
        self.assertEqual(settled_profit_truth(start=reporting_start).settled_cash, Decimal("0"))
        self.assertTrue(LedgerEntry.objects.filter(entry_key=f"payout:{payout.id}:reversed", event_type="PAYOUT_REVERSED").exists())
        self.assertTrue(order.settlement_events.filter(pk=settled_event.pk, state="SETTLED").exists())

    def test_owner_markets_and_money_show_two_sided_truth(self):
        market = self._ready_market()
        offering = self._prove(self._offering())
        self._receive(self._listing(offering, market), price="25", fee="2")
        market_row = next(row for row in markets_snapshot()["rows"] if row["market"] == "nevermined")
        self.assertEqual(market_row["category"], "SERVICE CHANNELS")
        self.assertIn("seller_capabilities", market_row)
        inbound_card = next(card for card in overview_snapshot()["cards"] if card["label"] == "INBOUND SERVICE EXPOSURE")
        self.assertEqual(inbound_card["value"], "$25.00")
        self.assertIn("not settled cash", inbound_card["truth"])
