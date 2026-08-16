import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TestCase

from control.models import (
    ApifyEventDefinition,
    ChannelEconomicsVersion,
    CommercialAPIProduct,
    CommercialAPIRequest,
    CommercialAPIUsage,
)
from control.services.commercial_api import (
    CommercialAPIError,
    apify_event_economics,
    bootstrap_commercial_catalog,
    buyer_for_external_reference,
    create_api_key,
    rapidapi_plan_economics,
    record_apify_charge,
    revoke_api_key,
)


class CommercialAPILaunchIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        bootstrap_commercial_catalog()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "AMARKTAI_UPLOAD_ROOT": str(Path(self.temp.name) / "uploads"),
            "AMARKTAI_JOB_ROOT": str(Path(self.temp.name) / "jobs"),
            "AMARKTAI_ENV": "test",
            "AUTONOMOUS_MODE": "OFF",
        }, clear=False)
        self.env.start(); self.addCleanup(self.env.stop)
        self.product = CommercialAPIProduct.objects.get(slug="data-cleanup")
        self.plan = self.product.plans.get(slug="basic")
        self.buyer = buyer_for_external_reference(channel="direct", external_reference="buyer-one")
        self.key, self.raw_key = create_api_key(buyer=self.buyer, plan=self.plan, label="test")

    def post(self, *, key=None, idem="request-1", payload=None, slug="data-cleanup"):
        return self.client.post(
            f"/api/v1/products/{slug}/jobs",
            data=json.dumps(payload if payload is not None else {"action": "normalize", "rows": [{" Name ": " Ada "}]}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {key or self.raw_key}",
            HTTP_IDEMPOTENCY_KEY=idem,
        )

    def test_authentication_unknown_product_and_invalid_schema_fail_closed(self):
        missing = self.client.post("/api/v1/products/data-cleanup/jobs", data="{}", content_type="application/json", HTTP_IDEMPOTENCY_KEY="x")
        self.assertEqual(missing.status_code, 401)
        unknown = self.post(slug="not-a-product")
        self.assertEqual(unknown.status_code, 404)
        invalid = self.post(payload={"action": "normalize"})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_SCHEMA")

    def test_revoked_key_fails_immediately(self):
        revoke_api_key(self.key)
        response = self.post()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "API_KEY_REVOKED")

    def test_quota_and_rate_limits_are_independent(self):
        self.plan.monthly_quota = 0
        self.plan.save(update_fields=["monthly_quota"])
        quota = self.post()
        self.assertEqual(quota.status_code, 429)
        self.assertEqual(quota.json()["error"]["code"], "QUOTA_EXHAUSTED")
        self.plan.monthly_quota = 25; self.plan.requests_per_minute = 1
        self.plan.save(update_fields=["monthly_quota", "requests_per_minute"])
        CommercialAPIRequest.objects.create(api_key=self.key, product=self.product, idempotency_key="prior", request_digest="x", correlation_id="prior")
        rate = self.post(idem="rate")
        self.assertEqual(rate.status_code, 429)
        self.assertEqual(rate.json()["error"]["code"], "RATE_LIMIT_EXCEEDED")

    def test_quota_is_reserved_at_admission_before_usage_settles(self):
        self.plan.monthly_quota = 1
        self.plan.requests_per_minute = 10
        self.plan.save(update_fields=["monthly_quota", "requests_per_minute"])
        with patch("control.commercial_views.execute_request", side_effect=lambda row: row):
            first = self.post(idem="quota-reservation-one")
            second = self.post(idem="quota-reservation-two")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["error"]["code"], "QUOTA_EXHAUSTED")

    def test_idempotency_replay_is_safe_and_conflict_is_rejected(self):
        with patch("control.commercial_views.execute_request", side_effect=lambda row: row):
            first = self.post(idem="same")
            replay = self.post(idem="same")
            conflict = self.post(idem="same", payload={"action": "normalize", "rows": [{"name": "Grace"}]})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay["Idempotent-Replay"], "true")
        self.assertEqual(first.json()["request_id"], replay.json()["request_id"])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(CommercialAPIRequest.objects.filter(api_key=self.key).count(), 1)

    def test_deterministic_success_is_qa_approved_and_metered_once(self):
        response = self.post(idem="deterministic-success")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["status"], "COMPLETED")
        self.assertTrue(payload["qa_passed"])
        self.assertIn("result", payload)
        request_row = CommercialAPIRequest.objects.get(pk=payload["request_id"])
        self.assertEqual(CommercialAPIUsage.objects.filter(request=request_row).count(), 1)
        replay = self.post(idem="deterministic-success")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(CommercialAPIUsage.objects.filter(request=request_row).count(), 1)

    def test_async_contract_queues_and_does_not_release_unaccepted_result(self):
        product = CommercialAPIProduct.objects.get(slug="document-text")
        _key, raw = create_api_key(buyer=self.buyer, plan=product.plans.get(slug="pro"))
        target = Mock()
        with patch("control.queueing.queue", return_value=target):
            response = self.post(key=raw, idem="async-one", slug=product.slug, payload={"filename": "note.txt", "content_base64": "aGVsbG8="})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "QUEUED")
        target.enqueue.assert_called_once()
        result = self.client.get(f"/api/v1/requests/{response.json()['request_id']}/result", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(result.status_code, 409)
        self.assertNotIn("result", result.json())

    def test_failed_worker_and_qa_failure_never_look_successful(self):
        def failed(row):
            row.status = CommercialAPIRequest.Status.FAILED; row.error_code = "EXECUTION_FAILED"; row.save(update_fields=["status", "error_code", "updated_at"]); return row
        with patch("control.commercial_views.execute_request", side_effect=failed):
            response = self.post(idem="worker-fail")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "EXECUTION_FAILED")
        row = CommercialAPIRequest.objects.get(idempotency_key="worker-fail")
        row.status = CommercialAPIRequest.Status.COMPLETED; row.qa_passed = False; row.save(update_fields=["status", "qa_passed"])
        result = self.client.get(f"/api/v1/requests/{row.id}/result", HTTP_AUTHORIZATION=f"Bearer {self.raw_key}")
        self.assertEqual(result.status_code, 409)

    def test_free_plan_cannot_trigger_paid_provider(self):
        paid = CommercialAPIProduct.objects.get(slug="structured-extraction")
        raw = create_api_key(buyer=self.buyer, plan=paid.plans.get(slug="basic"))[1]
        response = self.post(key=raw, idem="free-paid", slug=paid.slug, payload={"content": "Invoice 42", "schema": {"invoice": "string"}})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "FREE_PLAN_PAID_EXECUTION_BLOCKED")

    def test_underpriced_overage_is_rejected_before_execution(self):
        product = CommercialAPIProduct.objects.get(slug="document-text")
        plan = product.plans.get(slug="pro")
        plan.overage_price = Decimal("0.001")
        plan.save(update_fields=["overage_price"])
        raw = create_api_key(buyer=self.buyer, plan=plan)[1]
        response = self.post(
            key=raw,
            idem="underpriced",
            slug=product.slug,
            payload={"filename": "note.txt", "content_base64": "aGVsbG8="},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "ECONOMIC_ADMISSION_REJECTED")

    def test_rapidapi_proxy_identity_can_submit_and_poll_its_product(self):
        headers = {
            "HTTP_X_RAPIDAPI_PROXY_SECRET": "rapid-test-secret",
            "HTTP_X_RAPIDAPI_USER": "rapid-buyer-one",
            "HTTP_X_RAPIDAPI_SUBSCRIPTION": "pro",
        }
        with patch.dict(os.environ, {"RAPIDAPI_PROXY_SECRET": "rapid-test-secret"}, clear=False):
            response = self.client.post(
                "/api/v1/products/data-cleanup/jobs",
                data=json.dumps({"action": "normalize", "rows": [{" Name ": " Ada "}]}),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY="rapid-one",
                **headers,
            )
            self.assertEqual(response.status_code, 200, response.content)
            status = self.client.get(f"/api/v1/requests/{response.json()['request_id']}", **headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "COMPLETED")

    def test_async_queue_failure_becomes_terminal_safe_error(self):
        product = CommercialAPIProduct.objects.get(slug="document-text")
        raw = create_api_key(buyer=self.buyer, plan=product.plans.get(slug="pro"))[1]
        target = Mock()
        target.enqueue.side_effect = RuntimeError("redis unavailable")
        with patch("control.queueing.queue", return_value=target):
            response = self.post(
                key=raw,
                idem="queue-down",
                slug=product.slug,
                payload={"filename": "note.txt", "content_base64": "aGVsbG8="},
            )
        self.assertEqual(response.status_code, 503)
        row = CommercialAPIRequest.objects.get(idempotency_key="queue-down")
        self.assertEqual(row.status, CommercialAPIRequest.Status.FAILED)
        self.assertNotIn("redis", response.content.decode().lower())

    def test_rapidapi_and_apify_configured_economics_and_event_idempotency(self):
        calc = rapidapi_plan_economics(expected_calls=100, per_call_cost=Decimal("0.01"), desired_margin=Decimal("0.40"), fee_rate=Decimal("0.25"))
        self.assertEqual(calc["gross_price"], Decimal("2.8572"))
        self.assertEqual(calc["marketplace_fee"], Decimal("0.7143"))
        policy = ChannelEconomicsVersion.objects.get(channel="rapidapi")
        policy.marketplace_fee_rate = Decimal("0.20"); policy.save(update_fields=["marketplace_fee_rate"])
        changed = rapidapi_plan_economics(expected_calls=100, per_call_cost=Decimal("0.01"), desired_margin=Decimal("0.40"))
        self.assertEqual(changed["gross_price"], Decimal("2.5000"))
        event_calc = apify_event_economics(event_revenue=Decimal("1"), platform_usage_cost=Decimal("0.10"), external_cost=Decimal("0.10"), creator_share=Decimal("0.80"))
        self.assertEqual(event_calc["expected_profit"], Decimal("0.60"))
        definition = ApifyEventDefinition.objects.first()
        first, created = record_apify_charge(definition=definition, run_reference="run-1", charge_identity="charge-1", units=1, platform_usage_cost=Decimal("0.01"))
        replay, replay_created = record_apify_charge(definition=definition, run_reference="run-1", charge_identity="charge-1", units=1, platform_usage_cost=Decimal("0.01"))
        self.assertTrue(created); self.assertFalse(replay_created); self.assertEqual(first.pk, replay.pk)
        with self.assertRaises(CommercialAPIError):
            record_apify_charge(definition=definition, run_reference="run-other", charge_identity="charge-1", units=2, platform_usage_cost=Decimal("0.01"))
        apify_policy = ChannelEconomicsVersion.objects.get(channel="apify")
        apify_policy.verified = False
        apify_policy.save(update_fields=["verified"])
        with self.assertRaisesRegex(CommercialAPIError, "ECONOMICS_CATALOG_STALE"):
            apify_event_economics(event_revenue=Decimal("1"), platform_usage_cost=Decimal("0.1"))

    def test_openapi_is_generated_from_all_canonical_products(self):
        response = self.client.get("/api/openapi.json")
        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        for product in CommercialAPIProduct.objects.filter(enabled=True):
            self.assertIn(f"/api/v1/products/{product.slug}/jobs", paths)
