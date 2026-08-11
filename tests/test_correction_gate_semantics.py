from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal

import requests
from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.utils import timezone

from control.models import (
    Alert,
    CommercePayment,
    GenXCall,
    GenXCreditValuation,
    InboundSettlementEvent,
    Job,
    JobScore,
    MarketIntegrationProfile,
    Marketplace,
    ModelStat,
    OwnerReceipt,
    Payout,
    ProductCandidate,
    ServiceOffering,
)
from control.services.genx_valuation import record_credit_valuation
from control.services.integration_accounts import store_credentials
from control.services.paystack_commerce import (
    PaystackCommerceError,
    dispatch_webhook,
    initialize_checkout,
    reconcile_paystack_settlements,
)
from control.services.semantic_acceptance import (
    genx_cost_truth_is_fail_closed,
    genx_quality_and_exploration_bounded,
    genx_unit_safe_routing,
    paid_paths_use_economic_selection,
    paystack_charge_is_not_settlement,
    paystack_settlement_is_provider_proven,
    proof_runner_has_meaningful_stages,
)
from gateways.genx.contracts import price_hint, pricing_credit_estimate
from gateways.genx.service import GenXGateway


KEY = Fernet.generate_key().decode()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, malformed=False):
        self.status_code = status_code
        self.payload = payload
        self.malformed = malformed

    def json(self):
        if self.malformed:
            raise ValueError("malformed")
        return self.payload


class FakeSession:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self.error:
            raise self.error
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)


class SemanticAcceptanceContractTests(TestCase):
    def test_semantic_gate_detects_corrected_contracts(self):
        self.assertTrue(genx_unit_safe_routing())
        self.assertTrue(genx_quality_and_exploration_bounded())
        self.assertTrue(genx_cost_truth_is_fail_closed())
        self.assertTrue(paid_paths_use_economic_selection())
        self.assertTrue(paystack_charge_is_not_settlement())
        self.assertTrue(paystack_settlement_is_provider_proven())
        self.assertTrue(proof_runner_has_meaningful_stages())

    def test_pricing_requires_known_metric_and_request_unit(self):
        payload = {"unrelated_minimum_price": "0.0001", "credits_per_image": "2"}
        self.assertIsNone(price_hint(payload))
        self.assertEqual(
            pricing_credit_estimate(payload, {"image_count": 3}, historical_average=None, reserved_envelope=Decimal("9")),
            Decimal("6"),
        )
        self.assertEqual(
            pricing_credit_estimate({"input_price": "0.001"}, {}, historical_average=None, reserved_envelope=Decimal("9")),
            Decimal("9"),
        )


class GenXValuationTruthTests(TestCase):
    def setUp(self):
        market = Marketplace.objects.create(slug="valuation-market", display_name="Valuation market")
        self.job = Job.objects.create(
            marketplace=market,
            external_id="valuation-job",
            title="Valuation product",
            task_class="image_generation",
            reward=Decimal("10"),
            currency="USD",
            state=Job.State.EXECUTING,
        )
        self.product = ProductCandidate.objects.create(
            slug="valuation-product",
            product_class="IMAGE_ASSET",
            title="Valuation product",
            target_buyer="Test buyer",
            job=self.job,
            suggested_price=Decimal("10"),
            expected_gross=Decimal("10"),
            expected_cost=Decimal("2"),
            expected_net=Decimal("8"),
            expected_margin=Decimal("0.8"),
            confidence=Decimal("0.9"),
            max_genx_credits=Decimal("10"),
            payout_received=Decimal("10"),
        )
        JobScore.objects.create(
            job=self.job,
            p_acquire=Decimal("1"),
            p_accept=Decimal("1"),
            p_payment=Decimal("1"),
            expected_profit=Decimal("8"),
            expected_minutes=5,
            max_genx_credits=Decimal("10"),
        )
        self.call = GenXCall.objects.create(
            job=self.job,
            model="image-provider/model",
            task_class="image_generation",
            external_job_id="remote-valued",
            estimated_credits=Decimal("4"),
            max_allowed_credits=Decimal("10"),
            status="SUBMITTED",
            requested_metadata={"billing_truth": "PENDING", "accounting_currency": "USD"},
        )

    def test_actual_credits_remain_unresolved_then_revalue_product_exactly_once(self):
        payload = {"job_id": "remote-valued", "status": "completed", "usage": {"credits": "3"}}
        gateway = GenXGateway(client=object())
        gateway.reconcile_remote_job_payload(self.call.id, payload, source="OPERATOR_EVIDENCE")
        self.call.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.call.credits, Decimal("3"))
        self.assertIsNone(self.call.cost_equivalent)
        self.assertEqual(self.call.requested_metadata["cost_equivalent_truth"], "UNRESOLVED_VALUATION")
        self.assertFalse(self.product.cost_basis_resolved)
        self.assertEqual(self.product.net_profit, Decimal("0"))
        self.assertTrue(Alert.objects.filter(alert_type="GENX_CREDIT_VALUATION_MISSING").exists())

        valuation = record_credit_valuation(
            version="owner-invoice-2026-08",
            currency="USD",
            monetary_cost_per_credit=Decimal("2"),
            source="Owner-verified GenX acquisition invoice",
            effective_at=timezone.now() - timedelta(days=1),
            evidence={"invoice_digest": "sha256:test-evidence"},
            verified=True,
            actor="owner",
        )
        gateway.reconcile_remote_job_payload(self.call.id, payload, source="OPERATOR_EVIDENCE")
        gateway.reconcile_remote_job_payload(self.call.id, payload, source="OPERATOR_EVIDENCE")
        self.call.refresh_from_db()
        self.product.refresh_from_db()
        stat = ModelStat.objects.get(model=self.call.model, task_class=self.call.task_class)
        self.assertEqual(self.call.cost_equivalent, Decimal("6"))
        self.assertEqual(self.call.requested_metadata["valuation_version"], valuation.version)
        self.assertEqual(stat.cost_equivalent, Decimal("6"))
        self.assertTrue(self.product.cost_basis_resolved)
        self.assertEqual(self.product.cost_basis, Decimal("6"))
        self.assertEqual(self.product.net_profit, Decimal("4"))

    def test_unverified_valuation_is_never_used(self):
        GenXCreditValuation.objects.create(
            version="unverified",
            currency="USD",
            monetary_cost_per_credit=Decimal("1"),
            source="unverified assertion",
            evidence={"reference": "not-reviewed"},
            effective_at=timezone.now() - timedelta(days=1),
            verified=False,
        )
        GenXGateway(client=object()).reconcile_remote_job_payload(
            self.call.id,
            {"job_id": "remote-valued", "status": "completed", "usage": {"credits": "1"}},
            source="POLL",
        )
        self.call.refresh_from_db()
        self.assertIsNone(self.call.cost_equivalent)


@override_settings(FIELD_ENCRYPTION_ACTIVE_KID="v1", FIELD_ENCRYPTION_KEYS={"v1": KEY})
class PaystackSettlementTruthTests(TestCase):
    def setUp(self):
        store_credentials("paystack", {"secret_key": "sk_test_settlement"}, actor="owner")
        profile = MarketIntegrationProfile.objects.select_related("marketplace").get(marketplace__slug="paystack")
        profile.api_connection_state = "VERIFIED"
        profile.marketplace.enabled = True
        profile.marketplace.status = Marketplace.Status.LIVE
        profile.marketplace.payout_ready = True
        profile.marketplace.south_africa_verified = True
        profile.marketplace.save(update_fields=["enabled", "status", "payout_ready", "south_africa_verified", "updated_at"])
        profile.save(update_fields=["api_connection_state", "updated_at"])
        self.offering = ServiceOffering.objects.create(
            slug="settlement-proof",
            display_name="Settlement proof",
            capability="content_copy",
            operation="content_package",
            worker_class="content_copy",
            pricing_model=ServiceOffering.PricingModel.FIXED_PROJECT,
            currency="ZAR",
            advertised_price=Decimal("100"),
            minimum_profitable_price=Decimal("30"),
            expected_genx_cost=Decimal("5"),
            max_genx_credits=Decimal("5"),
            expected_operational_cost=Decimal("5"),
            input_schema={"type": "object"},
            output_schema={"type": "artifact"},
        )
        session = FakeSession([FakeResponse(payload={"status": True, "data": {"authorization_url": "https://checkout.paystack.com/test", "access_code": "test", "reference": "placeholder"}})])
        original = session.request

        def dynamic(method, url, **kwargs):
            response = original(method, url, **kwargs)
            response.payload["data"]["reference"] = kwargs["json"]["reference"]
            return response

        session.request = dynamic
        self.payment = initialize_checkout(
            offering_slug=self.offering.slug,
            customer_email="buyer@example.com",
            idempotency_key="settlement-proof",
            proof_mode=True,
            session=session,
        )
        self.payment.evidence = {**self.payment.evidence, "proof_mode": False}
        self.payment.save(update_fields=["evidence", "updated_at"])
        self._charge()

    def _signed(self, payload):
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return body, hmac.new(b"sk_test_settlement", body, hashlib.sha512).hexdigest()

    def _charge(self):
        body, signature = self._signed({
            "event": "charge.success",
            "data": {"id": 501, "reference": self.payment.external_reference, "status": "success", "amount": 10000, "currency": "ZAR", "fees": 448, "domain": "live"},
        })
        dispatch_webhook(raw_body=body, signature=signature)
        self.payment.refresh_from_db()

    def _settlement_session(self, status="success", reference=None, malformed=False):
        settlement = {
            "id": 9001,
            "domain": "live",
            "status": status,
            "currency": "ZAR",
            "effective_amount": 9552,
            "total_amount": 9552,
            "total_fees": 448,
            "total_processed": 10000,
            "deductions": None,
            "settlement_date": "2026-08-12T08:00:00Z",
        }
        list_response = FakeResponse(payload={"status": True, "data": [settlement], "meta": {"pageCount": 1}})
        if status != "success":
            return FakeSession([list_response])
        transaction = {
            "id": 501,
            "domain": "live",
            "status": "success",
            "reference": reference or self.payment.external_reference,
            "amount": 10000,
            "fees": 448,
            "currency": "ZAR",
        }
        tx_response = FakeResponse(payload={"status": True, "data": [transaction], "meta": {"pageCount": 1}}, malformed=malformed)
        return FakeSession([list_response, tx_response])

    def test_charge_creates_paid_funded_and_pending_truth_but_no_receipt(self):
        payout = Payout.objects.get(job_id=self.payment.order.job_id)
        self.assertEqual(self.payment.state, CommercePayment.State.PAID)
        self.assertTrue(self.payment.authoritative)
        self.assertEqual(self.payment.order.funding_state, "FUNDED")
        self.assertEqual(payout.state, Payout.State.PAYOUT_PENDING)
        self.assertFalse(OwnerReceipt.objects.filter(authoritative=True).exists())
        self.assertFalse(InboundSettlementEvent.objects.filter(state=InboundSettlementEvent.State.SETTLED).exists())

    def test_pending_and_processing_never_become_cash_ready(self):
        for status in ("pending", "processing"):
            with self.subTest(status=status):
                result = reconcile_paystack_settlements(session=self._settlement_session(status=status))
                self.assertEqual(result["pending"], 1)
                self.assertFalse(OwnerReceipt.objects.filter(state=OwnerReceipt.State.FIAT_SETTLED).exists())
                self.assertEqual(Payout.objects.get(job_id=self.payment.order.job_id).state, Payout.State.PAYOUT_PENDING)

    def test_successful_exact_settlement_is_idempotent_fiat_truth(self):
        first = reconcile_paystack_settlements(session=self._settlement_session())
        second = reconcile_paystack_settlements(session=self._settlement_session())
        payout = Payout.objects.get(job_id=self.payment.order.job_id)
        self.assertEqual(first["settled"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(payout.state, Payout.State.SETTLED)
        self.assertEqual(OwnerReceipt.objects.filter(state=OwnerReceipt.State.FIAT_SETTLED, authoritative=True).count(), 1)
        self.assertEqual(InboundSettlementEvent.objects.filter(state=InboundSettlementEvent.State.SETTLED).count(), 1)

    def test_unrelated_malformed_auth_and_timeout_fail_closed(self):
        unrelated = reconcile_paystack_settlements(session=self._settlement_session(reference="unrelated-reference"))
        self.assertEqual(unrelated["unrelated"], 1)
        self.assertFalse(OwnerReceipt.objects.filter(state=OwnerReceipt.State.FIAT_SETTLED).exists())
        malformed = FakeSession([FakeResponse(payload={"status": True, "data": "not-a-list"})])
        with self.assertRaisesRegex(PaystackCommerceError, "malformed"):
            reconcile_paystack_settlements(session=malformed)
        with self.assertRaises(PaystackCommerceError):
            reconcile_paystack_settlements(session=FakeSession(error=requests.Timeout()))
        self.assertFalse(OwnerReceipt.objects.filter(state=OwnerReceipt.State.FIAT_SETTLED).exists())
        with self.assertRaisesRegex(PaystackCommerceError, "rejected"):
            reconcile_paystack_settlements(session=FakeSession([FakeResponse(status_code=401, payload={})]))
        profile = MarketIntegrationProfile.objects.get(marketplace__slug="paystack")
        self.assertEqual(profile.api_connection_state, "UNVERIFIED")
        self.assertFalse(profile.marketplace.enabled)

    def test_refund_before_settlement_blocks_receipt_and_after_settlement_reverses(self):
        refund_body, refund_signature = self._signed({
            "event": "refund.processed",
            "data": {"transaction_reference": self.payment.external_reference, "status": "processed", "amount": 10000, "currency": "ZAR", "domain": "live"},
        })
        dispatch_webhook(raw_body=refund_body, signature=refund_signature)
        result = reconcile_paystack_settlements(session=self._settlement_session())
        self.assertEqual(result["settled"], 0)
        self.assertEqual(Payout.objects.get(job_id=self.payment.order.job_id).state, Payout.State.REVERSED)
        self.assertFalse(OwnerReceipt.objects.filter(state=OwnerReceipt.State.FIAT_SETTLED).exists())

    def test_refund_after_settlement_reverses_accounting(self):
        reconcile_paystack_settlements(session=self._settlement_session())
        refund_body, refund_signature = self._signed({
            "event": "refund.processed",
            "data": {"transaction_reference": self.payment.external_reference, "status": "processed", "amount": 10000, "currency": "ZAR", "domain": "live"},
        })
        dispatch_webhook(raw_body=refund_body, signature=refund_signature)
        payout = Payout.objects.get(job_id=self.payment.order.job_id)
        self.assertEqual(payout.state, Payout.State.REVERSED)
        self.assertTrue(OwnerReceipt.objects.filter(state=OwnerReceipt.State.REVERSED, authoritative=True).exists())

    def test_test_domain_refund_never_creates_production_receipt(self):
        self.payment.evidence = {**self.payment.evidence, "proof_mode": True, "domain": "test"}
        self.payment.save(update_fields=["evidence", "updated_at"])
        refund_body, refund_signature = self._signed({
            "event": "refund.processed",
            "data": {"transaction_reference": self.payment.external_reference, "status": "processed", "amount": 10000, "currency": "ZAR", "domain": "test"},
        })
        dispatch_webhook(raw_body=refund_body, signature=refund_signature)
        self.assertFalse(OwnerReceipt.objects.filter(state=OwnerReceipt.State.REVERSED, authoritative=True).exists())
