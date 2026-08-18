from decimal import Decimal

from django.test import TestCase

from control.models import (
    CommercialAPIRequest,
    CommercialAPIUsage,
    ConversionEvent,
    Job,
    JobScore,
    Marketplace,
    OfferExperiment,
    OfferVariant,
    OpportunityDecision,
)
from control.services.commercial_api import (
    bootstrap_commercial_catalog,
    buyer_for_external_reference,
    create_api_key,
)
from control.services.commercial_intelligence import (
    bootstrap_commercial_packages,
    bounded_customer_value_contribution,
    evaluate_capability_candidate,
    launch_inventory,
    profit_explanation_rows,
    recommend_experiment_winner,
    record_conversion_event,
    record_offer_event,
    refresh_buyer_profile,
)


class CommercialIntelligenceIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        bootstrap_commercial_catalog(); bootstrap_commercial_packages()

    def setUp(self):
        self.product = self._product()
        self.buyer = buyer_for_external_reference(channel="direct", external_reference="buyer-a")
        self.plan = self.product.plans.get(slug="pro")
        self.key, _ = create_api_key(buyer=self.buyer, plan=self.plan)

    def _product(self):
        from control.models import CommercialAPIProduct
        return CommercialAPIProduct.objects.get(slug="data-cleanup")

    def _settled_usage(self, idem, gross, fee, cost, profit):
        request = CommercialAPIRequest.objects.create(api_key=self.key, product=self.product, idempotency_key=idem, request_digest=idem, correlation_id=idem, status=CommercialAPIRequest.Status.COMPLETED, qa_passed=True)
        return CommercialAPIUsage.objects.create(request=request, buyer=self.buyer, product=self.product, plan=self.plan, gross_billed=gross, marketplace_fee=fee, execution_cost=cost, settled_revenue=gross, settled_net_profit=profit, authoritative_settlement=True)

    def test_customer_aggregation_repeat_state_and_idempotent_refresh(self):
        self._settled_usage("one", Decimal("10"), Decimal("2"), Decimal("1"), Decimal("7"))
        self._settled_usage("two", Decimal("20"), Decimal("4"), Decimal("2"), Decimal("14"))
        first = refresh_buyer_profile(channel="direct", external_reference="buyer-a")
        second = refresh_buyer_profile(channel="direct", external_reference="buyer-a")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.orders, 2); self.assertEqual(second.settled_orders, 2)
        self.assertEqual(second.settled_gross, Decimal("30")); self.assertEqual(second.settled_net_profit, Decimal("21"))
        self.assertTrue(second.repeat_buyer)

    def test_speculative_ltv_never_overrides_hard_loss(self):
        self.buyer.ltv_estimate = Decimal("100000"); self.buyer.ltv_confidence = Decimal("1"); self.buyer.sample_count = 100; self.buyer.save()
        decision = bounded_customer_value_contribution(immediate_profit=Decimal("-0.01"), customer=self.buyer, acquisition_budget=Decimal("500"), policy_enabled=True)
        self.assertFalse(decision["allowed"]); self.assertEqual(decision["contribution"], 0)
        bounded = bounded_customer_value_contribution(immediate_profit=Decimal("100"), customer=self.buyer, acquisition_budget=Decimal("1000"), policy_enabled=True)
        self.assertLessEqual(bounded["contribution"], Decimal("10"))

    def test_experiment_attribution_is_idempotent_and_winner_uses_settled_profit(self):
        experiment = OfferExperiment.objects.create(slug="pricing-copy", minimum_exposures=2, minimum_settled_outcomes=1)
        a = OfferVariant.objects.create(experiment=experiment, slug="a", presentation={"title": "A"})
        b = OfferVariant.objects.create(experiment=experiment, slug="b", presentation={"title": "B"})
        for variant in (a, b):
            record_offer_event(variant=variant, event_id=f"{variant.slug}-i1", event_type="IMPRESSION", anonymous_reference="one")
            record_offer_event(variant=variant, event_id=f"{variant.slug}-i2", event_type="PRODUCT_VIEW", anonymous_reference="two")
        settled, created = record_offer_event(variant=a, event_id="a-settled", event_type="SETTLED", authoritative=True, settled_gross=Decimal("10"), settled_cost=Decimal("2"))
        replay, replay_created = record_offer_event(variant=a, event_id="a-settled", event_type="SETTLED", authoritative=True, settled_gross=Decimal("10"), settled_cost=Decimal("2"))
        record_offer_event(variant=b, event_id="b-settled", event_type="SETTLED", authoritative=True, settled_gross=Decimal("6"), settled_cost=Decimal("1"))
        self.assertTrue(created); self.assertFalse(replay_created); self.assertEqual(settled.pk, replay.pk)
        result = recommend_experiment_winner(experiment)
        self.assertEqual(result["winner"]["variant"], "a")
        clicks_only = OfferExperiment.objects.create(slug="clicks-only", minimum_exposures=1, minimum_settled_outcomes=1)
        c = OfferVariant.objects.create(experiment=clicks_only, slug="clickbait")
        record_offer_event(variant=c, event_id="click", event_type="IMPRESSION")
        self.assertIsNone(recommend_experiment_winner(clicks_only)["winner"])

    def test_non_authoritative_settled_offer_is_rejected(self):
        experiment = OfferExperiment.objects.create(slug="auth-boundary")
        variant = OfferVariant.objects.create(experiment=experiment, slug="a")
        with self.assertRaisesRegex(ValueError, "AUTHORITATIVE"):
            record_offer_event(variant=variant, event_id="not-settled", event_type="SETTLED", authoritative=False, settled_gross=Decimal("5"))

    def test_conversion_telemetry_is_private_cautious_and_idempotent(self):
        first, created = record_conversion_event(event_id="evt-1", event_type="CTA_CLICK", anonymous_reference="browser-1", product_slug=self.product.slug, source="direct", metadata={"path": "/", "email": "discard@example.test"})
        replay, replay_created = record_conversion_event(event_id="evt-1", event_type="CTA_CLICK", anonymous_reference="browser-1", product_slug=self.product.slug)
        self.assertTrue(created); self.assertFalse(replay_created); self.assertEqual(first.pk, replay.pk)
        self.assertNotIn("email", first.metadata); self.assertNotEqual(first.anonymous_reference_hash, "browser-1")
        self.assertEqual(ConversionEvent.objects.count(), 1)
        self.assertFalse(hasattr(first, "settled_revenue"))

    def test_capability_evaluation_blocks_cost_and_quality_regression(self):
        cost = evaluate_capability_candidate(operation="extract_structured_facts", capability="extraction", candidate_version="costly", baseline_version="base", fixture_key="invoice", quality_score=Decimal("0.91"), baseline_quality_score=Decimal("0.90"), monetary_cost=Decimal("0.20"), baseline_monetary_cost=Decimal("0.10"), latency_ms=100, baseline_latency_ms=100, evidence={"fixture": True})
        quality = evaluate_capability_candidate(operation="extract_structured_facts", capability="extraction", candidate_version="bad", baseline_version="base", fixture_key="invoice", quality_score=Decimal("0.70"), baseline_quality_score=Decimal("0.90"), monetary_cost=Decimal("0.05"), baseline_monetary_cost=Decimal("0.10"), latency_ms=100, baseline_latency_ms=100, evidence={"fixture": True})
        self.assertEqual(cost.decision, "REGRESSION"); self.assertEqual(quality.decision, "REGRESSION")

    def test_launch_inventory_prefers_repeatable_deterministic_package(self):
        rows = launch_inventory()
        self.assertGreaterEqual(len(rows), 5)
        self.assertEqual(rows[0]["slug"], "structured-data-cleanup")
        self.assertEqual(rows[0]["classification"], "LAUNCH_FIRST")
        self.assertEqual(rows[0]["demand_evidence"], "NOT_YET_PROVEN")

    def test_profit_explanation_uses_persisted_job_score_and_decision(self):
        market = Marketplace.objects.create(slug="explain-market", display_name="Explain Market", fee_rate=Decimal("0.10"))
        job = Job.objects.create(marketplace=market, external_id="explain-1", title="Explain this decision", reward=Decimal("100"), state=Job.State.AWARDED)
        JobScore.objects.create(job=job, p_acquire=1, p_accept=Decimal("0.80"), p_payment=Decimal("0.90"), expected_genx_cost=Decimal("4"), expected_external_cost=Decimal("2"), expected_profit=Decimal("70"), expected_profit_per_minute=Decimal("7"), expected_minutes=10, recommended_offer=Decimal("100"))
        OpportunityDecision.objects.create(job=job, growth_stage="BOOTSTRAP", utilization_state="AVAILABLE", allowed=False, expected_cash_profit=Decimal("70"), risk_adjusted_profit=Decimal("50"), reason_codes=["AUTONOMY_OFF"], details={"selection_rank": 1, "alternative_opportunity": "none"})
        row = profit_explanation_rows()[0]
        self.assertEqual(row["expected_gross"], "100.00"); self.assertEqual(row["final_decision"], "REJECT")
        self.assertEqual(row["reason_codes"], ["AUTONOMY_OFF"]); self.assertTrue(row["would_select_if_enabled"])
