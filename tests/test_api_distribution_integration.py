from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase

from control.models import BuyerProfile, CommercialAPIKey, CommercialAPIPlan, CommercialAPIProduct
from control.services.api_distribution import (
    API_MARKET_CHANNEL,
    POSTMAN_CHANNEL,
    ZYLA_CHANNEL,
    ZYLA_LAUNCH_PRODUCT_SLUGS,
    api_distribution_acceptance_report,
    api_distribution_snapshot,
    api_market_export,
    bootstrap_api_distribution,
    issue_marketplace_backend_key,
    postman_export,
    zyla_export,
)
from control.services.api_distribution_identity import contextualize_marketplace_identity
from control.services.commercial_api import (
    AuthenticatedAPIIdentity,
    CommercialAPIError,
    _fee_rate,
    admit_request,
    authenticate_api_key,
    create_api_key,
)


class APIDistributionIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.bootstrap = bootstrap_api_distribution()

    def test_bootstrap_keeps_one_product_and_plan_catalog(self):
        self.assertEqual(CommercialAPIProduct.objects.filter(enabled=True).count(), 5)
        self.assertFalse(CommercialAPIPlan.objects.filter(slug__in={"api-market-backend", "zyla-backend"}).exists())
        self.assertFalse(self.bootstrap["duplicate_plan_catalog_created"])
        for product in CommercialAPIProduct.objects.filter(enabled=True):
            self.assertTrue(product.plans.filter(slug="mega", active=True).exists())

    def test_api_market_export_covers_all_products_and_current_sourced_share(self):
        export = api_market_export()
        self.assertEqual(export["channel"], API_MARKET_CHANNEL)
        self.assertFalse(export["published"])
        self.assertFalse(export["external_mutation_allowed"])
        self.assertEqual(len(export["products"]), 5)
        self.assertEqual(export["economics"]["marketplace_fee_rate"], "0.200000")
        self.assertEqual(export["economics"]["creator_share_rate"], "0.800000")
        self.assertTrue(export["gateway_contract"]["mcp_endpoint_generated_by_marketplace"])
        self.assertEqual(export["gateway_contract"]["submit_usage"], "API=1;")
        self.assertEqual(export["gateway_contract"]["status_result_usage"], "API=0;")
        serialized = json.dumps(export)
        self.assertNotIn("ak_", serialized)

    def test_zyla_is_fail_closed_to_proven_synchronous_launch_product(self):
        export = zyla_export()
        self.assertEqual(export["channel"], ZYLA_CHANNEL)
        self.assertEqual({row["slug"] for row in export["products"]}, ZYLA_LAUNCH_PRODUCT_SLUGS)
        self.assertEqual(ZYLA_LAUNCH_PRODUCT_SLUGS, {"data-cleanup"})
        deferred = {row["slug"] for row in export["deferred_products"]}
        self.assertEqual(deferred, {"structured-extraction", "document-text", "public-web-extraction", "media-utility"})
        self.assertFalse(export["published"])
        self.assertEqual(export["economics"]["payout_rail"], "PAYPAL")
        self.assertEqual(export["economics"]["admission_fee_reserve_rate"], "0.250000")

    def test_postman_export_is_discovery_only_and_contains_no_real_secret(self):
        export = postman_export()
        self.assertEqual(export["channel"], POSTMAN_CHANNEL)
        self.assertFalse(export["revenue_source"])
        self.assertFalse(export["published"])
        self.assertFalse(export["contains_secret_material"])
        self.assertGreaterEqual(len(export["collection"]["item"]), 7)
        serialized = json.dumps(export["collection"])
        self.assertNotIn("Bearer ak_", serialized)
        self.assertNotIn('"value": "ak_', serialized)

    def test_marketplace_keys_are_product_scoped_and_source_attributed(self):
        api_key, raw = issue_marketplace_backend_key(channel=API_MARKET_CHANNEL, product_slug="data-cleanup")
        identity = authenticate_api_key(raw)
        self.assertEqual(identity.source, "API_MARKET_PROXY")
        self.assertEqual(identity.key.plan.product.slug, "data-cleanup")
        with self.assertRaises(ValueError):
            issue_marketplace_backend_key(channel=ZYLA_CHANNEL, product_slug="structured-extraction")

    def test_api_market_forwarded_buyer_identity_is_pseudonymous_and_stable(self):
        gateway_key, raw = issue_marketplace_backend_key(channel=API_MARKET_CHANNEL, product_slug="data-cleanup")
        identity = authenticate_api_key(raw)
        product = gateway_key.plan.product

        class Headers:
            def get(self, key, default=None):
                return {"X-Magicapi-User": "buyer-123"}.get(key, default)

        class Request:
            headers = Headers()

        one = contextualize_marketplace_identity(identity, Request(), product)
        two = contextualize_marketplace_identity(identity, Request(), product)
        self.assertEqual(one.key_id if hasattr(one, "key_id") else one.key.id, two.key.id)
        self.assertEqual(one.source, "API_MARKET_PROXY")
        self.assertEqual(one.key.buyer.channel, API_MARKET_CHANNEL)
        self.assertNotEqual(one.key.buyer.external_reference_hash, "buyer-123")

    def test_api_market_requires_forwarded_buyer_after_gateway_auth(self):
        gateway_key, raw = issue_marketplace_backend_key(channel=API_MARKET_CHANNEL, product_slug="data-cleanup")
        identity = authenticate_api_key(raw)

        class Headers:
            def get(self, key, default=None):
                return default

        class Request:
            headers = Headers()

        with self.assertRaises(CommercialAPIError) as raised:
            contextualize_marketplace_identity(identity, Request(), gateway_key.plan.product)
        self.assertEqual(raised.exception.status, 401)

    def test_fee_attribution_is_channel_specific_and_direct_is_zero(self):
        product = CommercialAPIProduct.objects.get(slug="data-cleanup")
        plan = product.plans.get(slug="mega", version=1)
        direct_buyer = BuyerProfile.objects.create(channel="direct", external_reference_hash="direct-buyer")
        direct_key, _ = create_api_key(buyer=direct_buyer, plan=plan, label="direct customer")
        self.assertEqual(_fee_rate(AuthenticatedAPIIdentity(direct_key, "DIRECT_API_KEY"), product), Decimal("0"))

        api_market_key, _ = issue_marketplace_backend_key(channel=API_MARKET_CHANNEL, product_slug=product.slug)
        zyla_key, _ = issue_marketplace_backend_key(channel=ZYLA_CHANNEL, product_slug=product.slug)
        self.assertEqual(_fee_rate(AuthenticatedAPIIdentity(api_market_key, "API_MARKET_PROXY"), product), Decimal("0.200000"))
        self.assertEqual(_fee_rate(AuthenticatedAPIIdentity(zyla_key, "ZYLA_PROXY"), product), Decimal("0.250000"))

    def test_api_market_request_uses_marketplace_job_and_buyer_scoped_idempotency(self):
        gateway_key, raw = issue_marketplace_backend_key(channel=API_MARKET_CHANNEL, product_slug="data-cleanup")
        identity = authenticate_api_key(raw)
        product = gateway_key.plan.product

        class Headers:
            def __init__(self, buyer):
                self.buyer = buyer

            def get(self, key, default=None):
                return {"X-Magicapi-User": self.buyer}.get(key, default)

        class Request:
            def __init__(self, buyer):
                self.headers = Headers(buyer)

        buyer_one = contextualize_marketplace_identity(identity, Request("buyer-one"), product)
        buyer_two = contextualize_marketplace_identity(identity, Request("buyer-two"), product)
        payload = {"action": "normalize", "rows": [{" Name ": "Ada"}]}
        row_one, created_one = admit_request(identity=buyer_one, product=product, idempotency_key="same-key", payload=payload, correlation_id="one")
        row_two, created_two = admit_request(identity=buyer_two, product=product, idempotency_key="same-key", payload=payload, correlation_id="two")
        self.assertTrue(created_one)
        self.assertTrue(created_two)
        self.assertNotEqual(row_one.api_key_id, row_two.api_key_id)
        self.assertEqual(row_one.job.marketplace.slug, API_MARKET_CHANNEL)
        self.assertEqual(row_two.job.marketplace.slug, API_MARKET_CHANNEL)

    def test_zyla_request_is_attributed_to_zyla_marketplace(self):
        key, raw = issue_marketplace_backend_key(channel=ZYLA_CHANNEL, product_slug="data-cleanup")
        identity = authenticate_api_key(raw)
        row, created = admit_request(
            identity=identity,
            product=key.plan.product,
            idempotency_key="zyla-one",
            payload={"action": "normalize", "rows": [{"A": "1"}]},
            correlation_id="zyla-one",
        )
        self.assertTrue(created)
        self.assertEqual(row.job.marketplace.slug, ZYLA_CHANNEL)

    def test_api_market_http_contract_bills_submit_once_and_polling_zero(self):
        _key, raw = issue_marketplace_backend_key(channel=API_MARKET_CHANNEL, product_slug="data-cleanup")
        client = Client()
        payload = json.dumps({"action": "normalize", "rows": [{" Name ": " Ada "}]})
        headers = {
            "HTTP_AUTHORIZATION": f"Bearer {raw}",
            "HTTP_X_MAGICAPI_USER": "buyer-http",
            "HTTP_X_REQUEST_ID": "market-request-1",
        }
        with patch("control.commercial_views.execute_request", side_effect=lambda row: row):
            first = client.post("/api/v1/products/data-cleanup/jobs", data=payload, content_type="application/json", **headers)
            second = client.post("/api/v1/products/data-cleanup/jobs", data=payload, content_type="application/json", **headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first["X-Magicapi-Billing"], "API=1;")
        self.assertEqual(first["Idempotent-Replay"], "false")
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second["X-Magicapi-Billing"], "API=0;")
        self.assertEqual(second["Idempotent-Replay"], "true")
        request_id = first.json()["request_id"]
        status = client.get(f"/api/v1/requests/{request_id}", **headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status["X-Magicapi-Billing"], "API=0;")

    def test_distribution_snapshot_is_one_backend_many_storefronts(self):
        snapshot = api_distribution_snapshot()
        self.assertTrue(snapshot["single_execution_engine"])
        self.assertTrue(snapshot["single_product_catalog"])
        self.assertEqual(
            set(snapshot["channels"]),
            {"direct", "rapidapi", API_MARKET_CHANNEL, ZYLA_CHANNEL, POSTMAN_CHANNEL, "apify-store"},
        )
        self.assertFalse(snapshot["external_mutation_allowed"])

    def test_distribution_acceptance_passes_without_external_side_effects(self):
        report = api_distribution_acceptance_report()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["summary"]["FAIL"], 0)
        self.assertFalse(report["external_mutations_performed"])
