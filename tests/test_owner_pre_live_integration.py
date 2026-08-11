from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

import requests
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from control.integration_views import integration_credentials_api, integration_proof_submission_api
from control.models import (
    AuditEvent,
    CommercePayment,
    Execution,
    GenXCall,
    InboundOrder,
    Job,
    MarketIntegrationProfile,
    Marketplace,
    MarketplaceCredential,
    ModelStat,
    OwnerReceipt,
    Payout,
    ProductCandidate,
    ServiceOffering,
    WebhookEvent,
)
from control.services.genx_economics import record_settlement_outcome
from control.services.integration_accounts import (
    BY_SLUG,
    integration_accounts_snapshot,
    revoke_credentials,
    store_credentials,
)
from control.services.integration_connections import test_connection as run_connection_test
from control.services.paystack_commerce import (
    PaystackCommerceError,
    dispatch_webhook,
    initialize_checkout,
)
from control.services.product_factory import (
    factory_snapshot,
    generate_product_candidates,
    product_factory_cycle,
    record_internal_execution_outcome,
    record_owned_product_publication,
    record_owned_product_sale,
)
from gateways.genx.contracts import ModelCandidate, route_models

KEY = Fernet.generate_key().decode()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, malformed=False):
        self.status_code = status_code
        self._payload = payload
        self._malformed = malformed

    def json(self):
        if self._malformed:
            raise ValueError("malformed")
        return self._payload


class FakeSession:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.responses.pop(0)


@override_settings(FIELD_ENCRYPTION_ACTIVE_KID="v1", FIELD_ENCRYPTION_KEYS={"v1": KEY})
class IntegrationCredentialSecurityTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="owner", password="Long-password-123!", is_staff=True)
        self.factory = RequestFactory()

    def test_all_required_accounts_have_one_canonical_fail_closed_definition(self):
        self.assertEqual(len(BY_SLUG), 20)
        snapshot = integration_accounts_snapshot()
        self.assertEqual(snapshot["meta"]["total"], 20)
        self.assertEqual(snapshot["meta"]["connections_verified"], 0)
        self.assertEqual(snapshot["meta"]["autonomy_ready"], 0)
        self.assertTrue(all(not row["connected"] and not row["cash_ready"] for row in snapshot["rows"]))

    def test_secret_is_encrypted_write_only_and_absent_from_audit(self):
        secret = "sk_test_super_secret_value_123456"
        row = store_credentials("paystack", {"secret_key": secret}, actor=str(self.owner.pk))
        stored = MarketplaceCredential.objects.get(marketplace__slug="paystack", credential_type="secret_key", active=True)
        self.assertNotIn(secret, stored.encrypted_value)
        self.assertNotIn(secret, json.dumps(row))
        self.assertNotIn(secret, json.dumps(list(AuditEvent.objects.values_list("metadata", flat=True))))
        self.assertTrue(stored.fingerprint)

    @patch("control.integration_views.verify_reauthentication", return_value=True)
    def test_credential_api_requires_owner_and_never_returns_plaintext(self, _verify):
        unauthenticated = self.factory.post("/api/integrations/paystack/credentials", data=json.dumps({"credentials": {"secret_key": "sk_test_hidden"}}), content_type="application/json")
        self.assertEqual(integration_credentials_api(unauthenticated, "paystack").status_code, 401)
        request = self.factory.post("/api/integrations/paystack/credentials", data=json.dumps({"credentials": {"secret_key": "sk_test_hidden"}, "password": "x", "code": "1"}), content_type="application/json")
        request.owner = self.owner
        response = integration_credentials_api(request, "paystack")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"sk_test_hidden", response.content)

    def test_private_wallet_and_bank_material_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "private_or_recovery_material_prohibited"):
            store_credentials("taskbounty", {"solana_usdc_address": "seed phrase abandon ability able about above absent"}, actor="owner")
        with self.assertRaisesRegex(ValueError, "unsupported_credential_fields"):
            store_credentials("paystack", {"bank_account_number": "123456789"}, actor="owner")

    def test_revocation_disarms_readiness_but_preserves_history(self):
        store_credentials("paystack", {"secret_key": "sk_test_revoke_me"}, actor="owner")
        profile = MarketIntegrationProfile.objects.get(marketplace__slug="paystack")
        profile.api_connection_state = "VERIFIED"
        profile.work_capability_state = "VERIFIED"
        profile.live_proving_state = "READY"
        profile.policy_verified = True
        profile.autonomous_acquisition_enabled = True
        profile.marketplace.enabled = True
        profile.marketplace.payout_ready = True
        profile.marketplace.save()
        profile.save()
        revoke_credentials("paystack", actor="owner")
        profile.refresh_from_db()
        profile.marketplace.refresh_from_db()
        self.assertFalse(profile.autonomous_acquisition_enabled)
        self.assertFalse(profile.marketplace.enabled)
        self.assertFalse(profile.marketplace.payout_ready)
        self.assertEqual(profile.api_connection_state, "REVOKED")
        self.assertTrue(MarketplaceCredential.objects.filter(marketplace__slug="paystack", active=False).exists())

    @patch("control.integration_views.verify_reauthentication", return_value=True)
    def test_owner_proof_submission_cannot_fabricate_verified_payout(self, _verify):
        request = self.factory.post("/api/integrations/paystack/proof", data=json.dumps({"proof_type": "PAYOUT_RECEIPT", "proof_reference": "owner-upload-ref", "password": "x", "code": "1"}), content_type="application/json")
        request.owner = self.owner
        response = integration_proof_submission_api(request, "paystack")
        self.assertEqual(response.status_code, 200)
        profile = MarketIntegrationProfile.objects.get(marketplace__slug="paystack")
        self.assertEqual(profile.payout_receipt_proof_state, "SUBMITTED")
        self.assertEqual(profile.live_proving_state, "BLOCKED")


@override_settings(FIELD_ENCRYPTION_ACTIVE_KID="v1", FIELD_ENCRYPTION_KEYS={"v1": KEY})
class ConnectorContractTests(TestCase):
    def setUp(self):
        store_credentials("paystack", {"secret_key": "sk_test_contract"}, actor="owner")

    def test_connection_success_auth_rate_limit_timeout_and_malformed(self):
        success = FakeSession([FakeResponse(payload={"status": True, "data": []})])
        self.assertTrue(run_connection_test("paystack", actor="owner", session=success).ok)
        cases = (
            (FakeSession([FakeResponse(status_code=401, payload={})]), "AUTHENTICATION"),
            (FakeSession([FakeResponse(status_code=429, payload={})]), "RATE_LIMIT"),
            (FakeSession(error=requests.Timeout()), "TIMEOUT"),
            (FakeSession([FakeResponse(payload=None, malformed=True)]), "MALFORMED_RESPONSE"),
        )
        for session, code in cases:
            with self.subTest(code=code):
                result = run_connection_test("paystack", actor="owner", session=session)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, code)


@override_settings(FIELD_ENCRYPTION_ACTIVE_KID="v1", FIELD_ENCRYPTION_KEYS={"v1": KEY})
class PaystackDirectCommerceTests(TestCase):
    def setUp(self):
        store_credentials("paystack", {"secret_key": "sk_test_paystack_webhook"}, actor="owner")
        profile = MarketIntegrationProfile.objects.get(marketplace__slug="paystack")
        profile.api_connection_state = "VERIFIED"
        profile.save(update_fields=["api_connection_state", "updated_at"])
        self.offering = ServiceOffering.objects.create(
            slug="paystack-proof-product",
            display_name="Paystack proof product",
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
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "artifact"},
        )

    def _initialize(self):
        session = FakeSession([FakeResponse(payload={"status": True, "data": {"authorization_url": "https://checkout.paystack.com/abc", "access_code": "abc", "reference": "placeholder"}})])
        original_request = session.request
        def dynamic(method, url, **kwargs):
            response = original_request(method, url, **kwargs)
            response._payload["data"]["reference"] = kwargs["json"]["reference"]
            return response
        session.request = dynamic
        return initialize_checkout(offering_slug=self.offering.slug, customer_email="buyer@example.com", idempotency_key="proof-1", proof_mode=True, session=session)

    def test_initialization_is_not_revenue_and_is_idempotent(self):
        payment = self._initialize()
        self.assertEqual(payment.state, CommercePayment.State.INITIALIZED)
        self.assertFalse(payment.authoritative)
        self.assertFalse(InboundOrder.objects.exists())
        self.assertNotIn("buyer@example.com", json.dumps(payment.evidence))
        with self.assertRaisesRegex(PaystackCommerceError, "different checkout intent"):
            initialize_checkout(
                offering_slug=self.offering.slug,
                customer_email="different@example.com",
                idempotency_key="proof-1",
                proof_mode=True,
                session=FakeSession(),
            )

    def test_signed_webhook_is_idempotent_and_refund_reverses_truth(self):
        payment = self._initialize()
        body = json.dumps({"event": "charge.success", "data": {"id": 42, "reference": payment.external_reference, "status": "success", "amount": 10000, "currency": "ZAR", "fees": 448, "domain": "test"}}, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(b"sk_test_paystack_webhook", body, hashlib.sha512).hexdigest()
        first = dispatch_webhook(raw_body=body, signature=signature)
        second = dispatch_webhook(raw_body=body, signature=signature)
        self.assertTrue(first["handled"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(InboundOrder.objects.count(), 1)
        self.assertEqual(WebhookEvent.objects.count(), 1)
        payment.refresh_from_db()
        self.assertEqual(payment.state, CommercePayment.State.PAID)
        self.assertFalse(Payout.objects.filter(job_id=payment.order.job_id).exists())
        self.assertFalse(OwnerReceipt.objects.filter(authoritative=True).exists())
        refund = json.dumps({"event": "refund.processed", "data": {"transaction_reference": payment.external_reference, "status": "processed", "amount": 10000, "currency": "ZAR", "domain": "test"}}, sort_keys=True, separators=(",", ":")).encode()
        refund_signature = hmac.new(b"sk_test_paystack_webhook", refund, hashlib.sha512).hexdigest()
        dispatch_webhook(raw_body=refund, signature=refund_signature)
        payment.refresh_from_db()
        payment.order.refresh_from_db()
        self.assertEqual(payment.state, CommercePayment.State.REVERSED)
        self.assertEqual(payment.order.status, InboundOrder.Status.REVERSED)

    def test_invalid_signature_cannot_mutate_business_state(self):
        payment = self._initialize()
        body = json.dumps({"event": "charge.success", "data": {"reference": payment.external_reference}}).encode()
        with self.assertRaisesRegex(Exception, "signature"):
            dispatch_webhook(raw_body=body, signature="bad")
        payment.refresh_from_db()
        self.assertEqual(payment.state, CommercePayment.State.INITIALIZED)
        self.assertFalse(InboundOrder.objects.exists())

    def test_partial_refund_is_unknown_until_reconciled(self):
        payment = self._initialize()
        paid = json.dumps({"event": "charge.success", "data": {"id": 43, "reference": payment.external_reference, "status": "success", "amount": 10000, "currency": "ZAR", "fees": 448, "domain": "test"}}, sort_keys=True, separators=(",", ":")).encode()
        dispatch_webhook(raw_body=paid, signature=hmac.new(b"sk_test_paystack_webhook", paid, hashlib.sha512).hexdigest())
        refund = json.dumps({"event": "refund.processed", "data": {"transaction_reference": payment.external_reference, "status": "processed", "amount": 2500, "currency": "ZAR", "domain": "test"}}, sort_keys=True, separators=(",", ":")).encode()
        result = dispatch_webhook(raw_body=refund, signature=hmac.new(b"sk_test_paystack_webhook", refund, hashlib.sha512).hexdigest())
        payment.refresh_from_db()
        payment.order.refresh_from_db()
        self.assertFalse(result["handled"])
        self.assertEqual(payment.state, CommercePayment.State.UNKNOWN)
        self.assertNotEqual(payment.order.status, InboundOrder.Status.REVERSED)


class GenXEconomicRouterTests(TestCase):
    def test_task_specific_quality_constrained_expected_profit_routing(self):
        cheap_unreliable = ModelCandidate("cheap", price_hint=Decimal("1"), attempts=20, qa_accepted=10, qa_rejected=10, repair_required=8, credits=Decimal("20"), total_repair_cost=Decimal("16"))
        stronger = ModelCandidate("stronger", price_hint=Decimal("3"), attempts=20, qa_accepted=19, qa_rejected=1, repair_required=1, credits=Decimal("60"), total_repair_cost=Decimal("2"))
        routes = route_models(
            [cheap_unreliable, stronger],
            expected_revenue=Decimal("100"),
            required_quality=Decimal("0.80"),
            max_genx_credits=Decimal("10"),
            monetary_cost_per_credit=Decimal("1"),
        )
        self.assertEqual([route.candidate.model_id for route in routes], ["stronger"])
        self.assertGreater(routes[0].expected_net_profit, 0)

    def test_model_revenue_requires_settlement_and_reversal_removes_it(self):
        market = Marketplace.objects.create(slug="model-cash-truth", display_name="Model cash truth")
        job = Job.objects.create(
            marketplace=market,
            external_id="economic-truth-1",
            title="Economic truth",
            task_class="image_generation",
            reward=Decimal("100"),
            state=Job.State.SETTLED,
        )
        GenXCall.objects.create(job=job, model="image-model", task_class="image_generation", status="COMPLETED")
        payout = Payout.objects.create(
            job=job,
            gross=Decimal("100"),
            fee=Decimal("5"),
            net=Decimal("95"),
            state=Payout.State.SETTLED,
        )
        self.assertEqual(record_settlement_outcome(payout=payout), 1)
        self.assertEqual(record_settlement_outcome(payout=payout), 0)
        stat = ModelStat.objects.get(model="image-model", task_class="image_generation")
        self.assertEqual(stat.revenue, Decimal("95"))
        payout.state = Payout.State.REVERSED
        payout.save(update_fields=["state", "updated_at"])
        self.assertEqual(record_settlement_outcome(payout=payout), 1)
        stat.refresh_from_db()
        self.assertEqual(stat.revenue, Decimal("0"))


class ProductFactoryTests(TestCase):
    def test_zero_credential_factory_builds_candidates_but_spends_nothing_by_default(self):
        result = product_factory_cycle()
        self.assertFalse(result["policy"]["enabled"])
        self.assertIsNone(result["admitted_opportunity_id"])
        self.assertFalse(result["paid_execution_started"])
        snapshot = factory_snapshot()
        self.assertGreaterEqual(len(snapshot["products"]), 5)
        self.assertGreater(snapshot["capability_matrix_count"], 0)
        self.assertFalse(CommercePayment.objects.exists())

    def test_qa_pass_creates_unpublished_inventory_without_revenue(self):
        generate_product_candidates()
        product = ProductCandidate.objects.get(slug="original-business-social-template-pack")
        opportunity = product.opportunities.get()
        market = Marketplace.objects.create(slug="owned-products-test", display_name="Owned products test")
        job = Job.objects.create(
            marketplace=market,
            external_id="internal-image-1",
            title=product.title,
            task_class="image_generate_product_asset",
            reward=product.expected_gross,
            state=Job.State.EXECUTING,
        )
        product.job = job
        product.state = ProductCandidate.State.ECONOMICS_APPROVED
        product.save(update_fields=["job", "state", "updated_at"])
        opportunity.job = job
        opportunity.state = "EXECUTING"
        opportunity.save(update_fields=["job", "state", "updated_at"])
        execution = Execution.objects.create(
            job=job,
            status="QA_PASSED",
            started_at=timezone.now(),
        )
        GenXCall.objects.create(
            job=job,
            model="image-model",
            task_class="image_generation",
            status="COMPLETED",
            cost_equivalent=Decimal("3"),
            requested_metadata={"billing_truth": "ACTUAL", "cost_equivalent_truth": "ACTUAL", "valuation_version": "test-fixture"},
        )
        execution.ended_at = timezone.now()
        execution.save(update_fields=["ended_at", "updated_at"])
        record_internal_execution_outcome(execution=execution, qa_passed=True)
        product.refresh_from_db()
        self.assertEqual(product.state, ProductCandidate.State.READY_TO_PUBLISH)
        self.assertEqual(product.inventory_quantity, 1)
        self.assertEqual(product.gross_revenue, Decimal("0"))
        self.assertEqual(product.payout_received, Decimal("0"))
        self.assertEqual(product.net_profit, Decimal("-3"))
        published = record_owned_product_publication(
            product_slug=product.slug,
            channel="lemon-squeezy",
            remote_listing_id="remote-product-1",
            remote_reference="https://store.example.test/product-1",
            actor="owner",
        )
        self.assertEqual(published.state, ProductCandidate.State.PUBLISHED)
        self.assertTrue(record_owned_product_sale(
            offering=product.offering,
            channel="lemon-squeezy",
            event_key="lemon:order-1",
            gross=Decimal("29"),
        ))
        product.refresh_from_db()
        self.assertEqual(product.sales, 1)
        self.assertEqual(product.gross_revenue, Decimal("29"))
        self.assertEqual(product.payout_received, Decimal("0"))
