import hashlib
import hmac
import json
import os
import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from control.jwt_auth import issue_access
from control.models import (
    Execution,
    InboundOrder,
    Job,
    MarketServiceListing,
    Marketplace,
    OwnerSecurityProfile,
    QAResult,
    ServiceOffering,
    Worker,
)
from control.services.channel_commercial import (
    bootstrap_channel_commercial_pricing,
    priority_channel_commercial_pricing_snapshot,
    set_owner_commercial_price,
)
from control.services.channel_onboarding import (
    priority_channel_onboarding_snapshot,
    reapply_priority_channel_onboarding,
    update_priority_channel_onboarding,
)
from control.services.channel_publication import (
    priority_manual_publication_blockers,
    record_priority_manual_publication,
)
from control.services.inbound_controller import (
    accept_inbound_order,
    auto_accept_ready_inbound_orders,
)
from control.services.inbound_portal import (
    issue_intake_token,
    resolve_intake_token,
    submit_buyer_intake,
)
from control.services.lemon_webhooks import dispatch_lemon_webhook
from control.services.seller_services import receive_inbound_order, refresh_service_offering_proof
from markets.revenue_catalog import bootstrap_revenue_market_catalog


class ChannelCompletionIntegrationTests(TestCase):
    maxDiff = None

    def setUp(self):
        bootstrap_revenue_market_catalog()
        call_command("bootstrap_channel_packages", verbosity=0)
        bootstrap_channel_commercial_pricing()
        self.owner = get_user_model().objects.create_user(
            username="channel-owner",
            password="Strong-Channel-Owner-Password-2026!",
            is_staff=True,
        )
        OwnerSecurityProfile.objects.create(user=self.owner)

    def authenticate(self):
        self.client.cookies["amarktai_access"] = issue_access(self.owner)

    def _record_execution_proof(self, offering):
        proof_market, _ = Marketplace.objects.get_or_create(slug="channel-proof-market", defaults={"display_name": "Channel proof"})
        proof_job = Job.objects.create(
            marketplace=proof_market,
            external_id=f"proof-{offering.slug}",
            title="Package execution proof",
            task_class=offering.capability,
            reward="1",
            normalized_payload={"operation": offering.operation},
        )
        worker = Worker.objects.create(
            id=f"proof-{offering.slug}"[:120],
            worker_class=offering.worker_class,
            version="1.0.0",
            status="READY",
        )
        execution = Execution.objects.create(
            job=proof_job,
            worker=worker,
            status="COMPLETED",
            result={"operation": offering.operation},
        )
        QAResult.objects.create(
            job=proof_job,
            execution=execution,
            check_type="independent",
            passed=True,
            score="1",
        )

    def _prove_offering(self, package_slug):
        offering = ServiceOffering.objects.get(slug=package_slug)
        self._record_execution_proof(offering)
        refresh_service_offering_proof(offering)
        offering.refresh_from_db()
        refresh_service_offering_proof(offering)
        offering.refresh_from_db()
        self.assertEqual(offering.proof_state, ServiceOffering.ProofState.SELLABLE)
        offering.enabled = True
        offering.accepting_orders = True
        offering.save(update_fields=["enabled", "accepting_orders", "updated_at"])
        return offering

    def _onboard_market(self, market_slug):
        row = next(item for item in priority_channel_onboarding_snapshot()["rows"] if item["market"] == market_slug)
        checks = {key: True for key in row["required_checks"]}
        return update_priority_channel_onboarding(
            market_slug,
            checks=checks,
            proof_reference=f"fixture-{market_slug}-account-and-terms-proof",
            notes="Integration-only proof fixture; no payout claim.",
            actor="test-owner",
        )

    def _simulate_final_banking_gate(self, market_slug):
        market = Marketplace.objects.get(slug=market_slug)
        market.enabled = True
        market.status = Marketplace.Status.LIVE
        market.payout_ready = True
        market.south_africa_verified = True
        market.save(update_fields=["enabled", "status", "payout_ready", "south_africa_verified", "updated_at"])
        return market

    def _publish_ready_package(self, package_slug, *, remote_id=None, public_price="99.00"):
        listing = MarketServiceListing.objects.select_related("marketplace").get(offering__slug=package_slug)
        self._prove_offering(package_slug)
        self._onboard_market(listing.marketplace.slug)
        self._simulate_final_banking_gate(listing.marketplace.slug)
        set_owner_commercial_price(package_slug, price=public_price, actor="test-owner")
        row = record_priority_manual_publication(
            package_slug,
            remote_listing_id=remote_id or f"remote-{package_slug}",
            remote_reference=f"https://example.invalid/{package_slug}",
            remote_version="v1",
            evidence_reference=f"manual-publication-proof-{package_slug}",
            actor="test-owner",
        )
        self.assertTrue(row["published"])
        return MarketServiceListing.objects.select_related("offering", "marketplace").get(offering__slug=package_slug)

    def test_commercial_pricing_is_distinct_from_draft_catalog_price_and_fail_closed(self):
        snapshot = priority_channel_commercial_pricing_snapshot()
        self.assertEqual(snapshot["meta"]["total_packages"], 17)
        self.assertEqual(snapshot["meta"]["priced_packages"], 16)
        self.assertEqual(snapshot["meta"]["blocked_packages"], 1)
        self.assertEqual(snapshot["meta"]["published_prices"], 0)
        apify = next(row for row in snapshot["rows"] if row["package_slug"] == "apify-website-data-extractor")
        self.assertFalse(apify["prepared"])
        self.assertIn("EXTERNAL_EXECUTION_COST_PROFILE_NOT_PROVEN", apify["blockers"])
        rapid = next(row for row in snapshot["rows"] if row["package_slug"] == "rapidapi-json-to-csv")
        self.assertIsNotNone(rapid["public_price"])
        self.assertIsNone(rapid["published_price"])
        self.assertNotEqual(rapid["catalog_listing_price_field"], rapid["published_price"])

    def test_owner_price_override_cannot_undercut_launch_minimum_and_survives_bootstrap(self):
        with self.assertRaisesRegex(ValueError, "COMMERCIAL_PRICE_BELOW_LAUNCH_MINIMUM"):
            set_owner_commercial_price("rapidapi-json-to-csv", price="0.25", actor="test-owner")
        row = set_owner_commercial_price("rapidapi-json-to-csv", price="1.25", actor="test-owner")
        self.assertEqual(row["public_price"], "1.25")
        self.assertTrue(row["owner_approved"])
        bootstrap_channel_commercial_pricing()
        refreshed = next(
            item for item in priority_channel_commercial_pricing_snapshot()["rows"]
            if item["package_slug"] == "rapidapi-json-to-csv"
        )
        self.assertEqual(refreshed["public_price"], "1.25")
        self.assertTrue(refreshed["owner_approved"])

    def test_onboarding_survives_static_catalog_refresh_without_touching_payout_truth(self):
        row = self._onboard_market("rapidapi")
        self.assertTrue(row["account_contract_ready"])
        self.assertTrue(row["policy_verified"])
        market = Marketplace.objects.get(slug="rapidapi")
        self.assertFalse(market.payout_ready)
        self.assertFalse(market.south_africa_verified)
        self.assertTrue(market.integration_profile.seller_capabilities["receive_orders"])

        bootstrap_revenue_market_catalog()
        market.refresh_from_db()
        self.assertFalse(market.payout_ready)
        self.assertFalse(market.south_africa_verified)
        reapply_priority_channel_onboarding()
        market.refresh_from_db()
        self.assertTrue(market.integration_profile.seller_capabilities["receive_orders"])
        self.assertTrue(market.integration_profile.seller_capabilities["publish_service"])
        self.assertFalse(market.payout_ready)
        self.assertFalse(market.south_africa_verified)

    def test_publication_is_banking_gated_then_records_owner_remote_evidence(self):
        package = "rapidapi-json-to-csv"
        listing = MarketServiceListing.objects.select_related("marketplace").get(offering__slug=package)
        self._prove_offering(package)
        self._onboard_market("rapidapi")
        set_owner_commercial_price(package, price="1.25", actor="test-owner")
        blockers = priority_manual_publication_blockers(listing)
        self.assertIn("PAYOUT_NOT_READY", blockers)
        self.assertIn("SOUTH_AFRICA_PAYOUT_NOT_VERIFIED", blockers)

        self._simulate_final_banking_gate("rapidapi")
        listing.refresh_from_db()
        self.assertEqual(priority_manual_publication_blockers(listing), [])
        published = record_priority_manual_publication(
            package,
            remote_listing_id="rapid-variant-1",
            remote_reference="https://example.invalid/rapidapi/rapid-variant-1",
            remote_version="1",
            evidence_reference="owner-confirmed-published-provider-listing",
            actor="test-owner",
        )
        self.assertTrue(published["published"])
        self.assertEqual(published["published_price"], "1.25")
        listing.refresh_from_db()
        self.assertEqual(listing.status, MarketServiceListing.Status.PUBLISHED)
        self.assertEqual(listing.remote_listing_id, "rapid-variant-1")
        self.assertFalse(listing.platform_metadata["publication_evidence"]["external_mutation_performed_by_amarktai"])

    def test_manual_mode_allows_owner_acceptance_but_never_auto_accepts(self):
        listing = self._publish_ready_package("rapidapi-json-to-csv", public_price="1.25")
        order, _ = receive_inbound_order(
            marketplace=listing.marketplace,
            listing=listing,
            remote_order_id="manual-test-order-1",
            idempotency_key="manual-test-order-1",
            payload={
                "buyer_reference": "fixture-buyer",
                "requirements": {"json_payload": '[{"a":1}]'},
                "input_assets": [],
                "quoted_price": "1.25",
                "platform_fee": "0.31",
                "currency": "USD",
                "funding_state": "FUNDED",
            },
            authenticated_market_identity=True,
            authenticated_at=timezone.now(),
        )
        self.assertEqual(order.status, InboundOrder.Status.READY)
        with patch.dict(os.environ, {"AUTONOMOUS_MODE": "MANUAL", "INBOUND_SERVICE_AUTO_ACCEPT_ENABLED": "0"}, clear=False):
            accepted = accept_inbound_order(order.id, actor="test-owner", manual=True)
            self.assertEqual(accepted.status, InboundOrder.Status.ACCEPTED)
            second, _ = receive_inbound_order(
                marketplace=listing.marketplace,
                listing=listing,
                remote_order_id="manual-test-order-2",
                idempotency_key="manual-test-order-2",
                payload={
                    "buyer_reference": "fixture-buyer",
                    "requirements": {"json_payload": '[{"a":2}]'},
                    "input_assets": [],
                    "quoted_price": "1.25",
                    "platform_fee": "0.31",
                    "currency": "USD",
                    "funding_state": "FUNDED",
                },
                authenticated_market_identity=True,
                authenticated_at=timezone.now(),
            )
            self.assertEqual(second.status, InboundOrder.Status.READY)
            auto = auto_accept_ready_inbound_orders(limit=10)
            self.assertEqual(auto["accepted"], 0)
            self.assertGreaterEqual(auto["skipped"], 1)

    def test_rapidapi_proxy_auth_idempotency_and_inline_asset_ingress(self):
        self._publish_ready_package("rapidapi-json-to-csv", public_price="1.25")
        with tempfile.TemporaryDirectory() as job_root, tempfile.TemporaryDirectory() as upload_root, patch.dict(
            os.environ,
            {
                "AUTONOMOUS_MODE": "LOW_RISK",
                "INBOUND_SERVICE_AUTO_ACCEPT_ENABLED": "1",
                "RAPIDAPI_PUBLIC_INGRESS_ENABLED": "1",
                "RAPIDAPI_PROXY_SECRET": "fixture-rapid-proxy-secret",
                "AMARKTAI_JOB_ROOT": job_root,
                "AMARKTAI_UPLOAD_ROOT": upload_root,
            },
            clear=False,
        ), patch("control.services.inbound_controller._queue_execution", return_value=True):
            bad = self.client.post(
                "/api/channels/rapidapi/rapidapi-json-to-csv",
                data={"request_id": "request-1", "json_payload": [{"a": 1}]},
                content_type="application/json",
                HTTP_X_RAPIDAPI_PROXY_SECRET="wrong",
                HTTP_X_RAPIDAPI_USER="buyer-1",
            )
            self.assertEqual(bad.status_code, 401)

            first = self.client.post(
                "/api/channels/rapidapi/rapidapi-json-to-csv",
                data={"request_id": "request-1", "json_payload": [{"a": 1}]},
                content_type="application/json",
                HTTP_X_RAPIDAPI_PROXY_SECRET="fixture-rapid-proxy-secret",
                HTTP_X_RAPIDAPI_USER="buyer-1",
            )
            self.assertEqual(first.status_code, 202, first.content)
            body = first.json()
            order_id = body["order_id"]
            order = InboundOrder.objects.get(pk=order_id)
            self.assertEqual(order.status, InboundOrder.Status.ACCEPTED)
            self.assertEqual(len(order.input_assets), 1)
            self.assertEqual(InboundOrder.objects.filter(marketplace__slug="rapidapi").count(), 1)

            second = self.client.post(
                "/api/channels/rapidapi/rapidapi-json-to-csv",
                data={"request_id": "request-1", "json_payload": [{"a": 1}]},
                content_type="application/json",
                HTTP_X_RAPIDAPI_PROXY_SECRET="fixture-rapid-proxy-secret",
                HTTP_X_RAPIDAPI_USER="buyer-1",
            )
            self.assertEqual(second.status_code, 202, second.content)
            self.assertEqual(second.json()["order_id"], order_id)
            self.assertEqual(InboundOrder.objects.filter(marketplace__slug="rapidapi").count(), 1)

            status = self.client.get(
                f"/api/channels/rapidapi/orders/{order_id}",
                HTTP_X_RAPIDAPI_PROXY_SECRET="fixture-rapid-proxy-secret",
                HTTP_X_RAPIDAPI_USER="buyer-1",
            )
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["order_id"], order_id)

    def test_signed_buyer_intake_completes_missing_contra_dataset(self):
        listing = self._publish_ready_package("contra-spreadsheet-report", public_price="99.00")
        with patch.dict(os.environ, {"AUTONOMOUS_MODE": "MANUAL"}, clear=False):
            response = self.client.post("/api/channels/contra/orders", data={}, content_type="application/json")
            self.assertEqual(response.status_code, 401)

        self.authenticate()
        with patch.dict(os.environ, {"AUTONOMOUS_MODE": "MANUAL"}, clear=False):
            imported = self.client.post(
                "/api/channels/contra/orders",
                data={
                    "package_slug": "contra-spreadsheet-report",
                    "remote_order_id": "contra-project-1",
                    "buyer_reference": "buyer-reference",
                    "quoted_price": "99.00",
                    "currency": "USD",
                    "requirements": {"report_goal": "Monthly operating report", "branding_preferences": "Clean and professional"},
                    "funding_state": "FUNDED",
                    "evidence_reference": "owner-observed-contra-project-contract",
                },
                content_type="application/json",
            )
            self.assertEqual(imported.status_code, 201, imported.content)
            order = InboundOrder.objects.get(pk=imported.json()["order"]["order_id"])
            self.assertEqual(order.remote_state, "AWAITING_BUYER_INPUT")

            token = issue_intake_token(order, actor="test-owner")
            resolved = resolve_intake_token(token)
            self.assertEqual(resolved.id, order.id)
            with tempfile.TemporaryDirectory() as job_root, tempfile.TemporaryDirectory() as upload_root, patch.dict(
                os.environ,
                {"AMARKTAI_JOB_ROOT": job_root, "AMARKTAI_UPLOAD_ROOT": upload_root},
                clear=False,
            ):
                completed = submit_buyer_intake(
                    resolved,
                    fields={},
                    uploads=[SimpleUploadedFile("dataset.csv", b"name,value\nA,1\nB,2\n", content_type="text/csv")],
                )
            self.assertEqual(completed.status, InboundOrder.Status.READY)
            self.assertEqual(len(completed.input_assets), 1)

    def _signed_lemon(self, payload, secret):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return raw, signature

    def test_lemon_subscription_maps_initial_order_and_creates_only_paid_renewal_cycles(self):
        listing = self._publish_ready_package("lemon-seo-subscription", remote_id="lemon-variant-1", public_price="49.00")
        secret = "fixture-lemon-webhook-secret"
        with patch.dict(os.environ, {"LEMON_SQUEEZY_WEBHOOK_SECRET": secret, "LEMON_SQUEEZY_WEBHOOK_ENABLED": "1"}, clear=False):
            order_payload = {
                "meta": {
                    "event_name": "order_created",
                    "custom_data": {
                        "package_slug": "lemon-seo-subscription",
                        "buyer_reference": "subscriber-1",
                        "requirements": {
                            "website_or_pages": "https://example.com",
                            "goals": "Improve organic search visibility",
                            "audience": "Small business owners",
                        },
                    },
                },
                "data": {
                    "id": "lemon-order-1",
                    "type": "orders",
                    "attributes": {
                        "status": "paid",
                        "currency": "USD",
                        "total": 4900,
                        "tax": 0,
                        "test_mode": True,
                        "first_order_item": {"variant_id": "lemon-variant-1"},
                    },
                },
            }
            raw, sig = self._signed_lemon(order_payload, secret)
            first = dispatch_lemon_webhook(raw_body=raw, signature=sig)
            self.assertTrue(first["created"])
            original_id = first["order_id"]

            subscription_payload = {
                "meta": {"event_name": "subscription_created"},
                "data": {
                    "id": "subscription-1",
                    "type": "subscriptions",
                    "attributes": {
                        "order_id": "lemon-order-1",
                        "variant_id": "lemon-variant-1",
                        "status": "active",
                    },
                },
            }
            raw, sig = self._signed_lemon(subscription_payload, secret)
            mapped = dispatch_lemon_webhook(raw_body=raw, signature=sig)
            self.assertEqual(mapped["order_id"], original_id)
            self.assertEqual(mapped["subscription_id"], "subscription-1")

            initial_invoice = {
                "meta": {"event_name": "subscription_payment_success"},
                "data": {
                    "id": "invoice-initial",
                    "type": "subscription-invoices",
                    "attributes": {
                        "subscription_id": "subscription-1",
                        "billing_reason": "initial",
                        "status": "paid",
                        "currency": "USD",
                        "total": 4900,
                        "tax": 0,
                    },
                },
            }
            raw, sig = self._signed_lemon(initial_invoice, secret)
            initial = dispatch_lemon_webhook(raw_body=raw, signature=sig)
            self.assertFalse(initial["renewal"])
            self.assertEqual(InboundOrder.objects.filter(marketplace__slug="lemon-squeezy").count(), 1)

            renewal_payload = {
                "meta": {"event_name": "subscription_payment_success"},
                "data": {
                    "id": "invoice-renewal-1",
                    "type": "subscription-invoices",
                    "attributes": {
                        "subscription_id": "subscription-1",
                        "billing_reason": "renewal",
                        "status": "paid",
                        "currency": "USD",
                        "total": 4900,
                        "tax": 0,
                        "test_mode": True,
                    },
                },
            }
            raw, sig = self._signed_lemon(renewal_payload, secret)
            renewal = dispatch_lemon_webhook(raw_body=raw, signature=sig)
            self.assertTrue(renewal["renewal"])
            self.assertTrue(renewal["created"])
            duplicate = dispatch_lemon_webhook(raw_body=raw, signature=sig)
            self.assertFalse(duplicate["created"])
            self.assertEqual(duplicate["order_id"], renewal["order_id"])
            self.assertEqual(InboundOrder.objects.filter(marketplace__slug="lemon-squeezy").count(), 2)

            with self.assertRaisesRegex(Exception, "LEMON_WEBHOOK_SIGNATURE_INVALID"):
                dispatch_lemon_webhook(raw_body=raw, signature="invalid")

    def test_new_owner_control_apis_are_owner_only(self):
        for path in ("/api/channels/onboarding", "/api/channels/publications", "/api/channels/commercial-pricing", "/api/channels/ingress", "/api/channels/orders"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.authenticate()
        self.assertEqual(self.client.get("/api/channels/onboarding").status_code, 200)
        self.assertEqual(self.client.get("/api/channels/publications").status_code, 200)
        self.assertEqual(self.client.get("/api/channels/commercial-pricing").status_code, 200)
        self.assertEqual(self.client.get("/api/channels/ingress").status_code, 200)
        self.assertEqual(self.client.get("/api/channels/orders").status_code, 200)
