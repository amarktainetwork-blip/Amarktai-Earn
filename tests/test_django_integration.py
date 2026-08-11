import json
from decimal import Decimal

import pyotp
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import Client, TestCase
from django.utils import timezone

from control.models import (
    GenXAccountSnapshot,
    GenXModelCatalog,
    Job,
    Marketplace,
    OwnerSecurityProfile,
    RecoveryCode,
    RefreshSession,
    WebhookEvent,
)
from control.secrets import encrypt_secret
from control.services.locks import JobLockUnavailable, acquire_job_lock, release_job_lock
from gateways.genx.service import GenXGateway
from markets.agentgigs.webhooks import signature_for


class FakeCatalogClient:
    def __init__(self, *, models, pricing, credits):
        self.models_payload = models
        self.pricing_payload = pricing
        self.credits_payload = credits
        self.calls = []

    def list_models(self, category=None):
        self.calls.append(("list_models", category))
        return self.models_payload

    def pricing(self, category=None):
        self.calls.append(("pricing", category))
        return self.pricing_payload

    def credits(self):
        self.calls.append(("credits", None))
        return self.credits_payload

    def generate(self, *args, **kwargs):
        raise AssertionError("catalog sync must never generate content")


class GenXCatalogIntegrationTests(TestCase):
    def test_string_models_merge_pricing_metadata_and_snapshot_credits(self):
        pricing_rows = [
            {
                "model": "text-model-a",
                "category": "text",
                "provider": "provider-a",
                "pricing": {"credits": 1},
            },
            {
                "model": "image-model-b",
                "category": "image",
                "provider": "provider-b",
                "pricing": {"credits": 2},
            },
        ]
        credits_payload = {"data": {"available_credits": "42.5"}}
        client = FakeCatalogClient(
            models={"data": ["text-model-a", "image-model-b"]},
            pricing={"data": pricing_rows},
            credits=credits_payload,
        )

        result = GenXGateway(client=client).sync_catalog()

        self.assertEqual(result, {"models_seen": 2, "available_credits": Decimal("42.5")})
        self.assertEqual(GenXModelCatalog.objects.filter(active=True).count(), 2)
        text_model = GenXModelCatalog.objects.get(model_id="text-model-a")
        image_model = GenXModelCatalog.objects.get(model_id="image-model-b")
        self.assertEqual((text_model.category, text_model.provider), ("text", "provider-a"))
        self.assertEqual((image_model.category, image_model.provider), ("image", "provider-b"))
        self.assertEqual(text_model.model_payload, {"id": "text-model-a"})
        self.assertEqual(image_model.model_payload, {"id": "image-model-b"})
        self.assertEqual(text_model.pricing_payload, pricing_rows[0])
        self.assertEqual(image_model.pricing_payload, pricing_rows[1])
        self.assertIsNone(text_model.price_hint)
        self.assertIsNone(image_model.price_hint)
        snapshot = GenXAccountSnapshot.objects.get()
        self.assertEqual(snapshot.available_credits, Decimal("42.5"))
        self.assertEqual(snapshot.raw, credits_payload)
        self.assertEqual(client.calls, [("list_models", None), ("pricing", None), ("credits", None)])

    def test_stale_deactivation_remains_category_scoped(self):
        GenXModelCatalog.objects.create(model_id="stale-text", category="text", active=True)
        GenXModelCatalog.objects.create(model_id="untouched-image", category="image", active=True)
        client = FakeCatalogClient(
            models={"data": ["current-text"]},
            pricing={
                "data": [
                    {
                        "model": "current-text",
                        "category": "text",
                        "provider": "provider-a",
                        "pricing": {"credits": 1},
                    }
                ]
            },
            credits={"available_credits": 10},
        )

        GenXGateway(client=client).sync_catalog(category="text")

        self.assertFalse(GenXModelCatalog.objects.get(model_id="stale-text").active)
        self.assertTrue(GenXModelCatalog.objects.get(model_id="untouched-image").active)
        self.assertTrue(GenXModelCatalog.objects.get(model_id="current-text").active)

    def test_empty_authoritative_model_ids_do_not_create_pricing_only_models(self):
        client = FakeCatalogClient(
            models={"data": [None, False, 0, "", "   "]},
            pricing={
                "data": [
                    {
                        "model": "pricing-only",
                        "category": "text",
                        "provider": "provider-a",
                        "pricing": {"credits": 1},
                    }
                ]
            },
            credits={"available_credits": 10},
        )

        result = GenXGateway(client=client).sync_catalog()

        self.assertEqual(result["models_seen"], 0)
        self.assertFalse(GenXModelCatalog.objects.exists())
        self.assertEqual(GenXAccountSnapshot.objects.count(), 1)


class OwnerAuthIntegrationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="owner",
            email="owner@example.test",
            password="Strong-Owner-Password-2026!",
            is_staff=True,
            is_superuser=True,
        )
        self.totp_secret = pyotp.random_base32()
        OwnerSecurityProfile.objects.create(
            user=self.user,
            totp_secret_encrypted=encrypt_secret(self.totp_secret),
            totp_confirmed_at=timezone.now(),
        )
        self.client = Client(enforce_csrf_checks=False)

    def _password_login(self):
        return self.client.post(
            "/api/auth/login",
            data=json.dumps({"username": "owner", "password": "Strong-Owner-Password-2026!"}),
            content_type="application/json",
        )

    def _totp_login(self):
        response = self._password_login()
        self.assertEqual(response.status_code, 200)
        self.assertIn(settings.PREAUTH_COOKIE_NAME, response.cookies)
        response = self.client.post(
            "/api/auth/totp",
            data=json.dumps({"code": pyotp.TOTP(self.totp_secret).now()}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(settings.ACCESS_COOKIE_NAME, response.cookies)
        self.assertIn(settings.REFRESH_COOKIE_NAME, response.cookies)
        self.assertTrue(response.cookies[settings.ACCESS_COOKIE_NAME]["httponly"])
        self.assertEqual(response.cookies[settings.ACCESS_COOKIE_NAME]["samesite"], "Strict")
        return response

    def test_password_then_totp_then_authenticated_overview(self):
        self._totp_login()
        response = self.client.get("/api/overview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["settled_today"], "0")
        self.assertIn("Only SETTLED", response.json()["revenue_truth"])

    def test_invalid_totp_does_not_issue_auth_tokens(self):
        self.assertEqual(self._password_login().status_code, 200)
        response = self.client.post(
            "/api/auth/totp",
            data=json.dumps({"code": "000000"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(settings.ACCESS_COOKIE_NAME, response.cookies)
        self.assertNotIn(settings.REFRESH_COOKIE_NAME, response.cookies)

    def test_refresh_rotates_and_reuse_revokes_family(self):
        login = self._totp_login()
        original_refresh = login.cookies[settings.REFRESH_COOKIE_NAME].value
        first = self.client.post("/api/auth/refresh", data="{}", content_type="application/json")
        self.assertEqual(first.status_code, 200)
        self.assertNotEqual(original_refresh, first.cookies[settings.REFRESH_COOKIE_NAME].value)

        attacker = Client(enforce_csrf_checks=False)
        attacker.cookies[settings.REFRESH_COOKIE_NAME] = original_refresh
        reused = attacker.post("/api/auth/refresh", data="{}", content_type="application/json")
        self.assertEqual(reused.status_code, 401)
        self.assertFalse(RefreshSession.objects.filter(revoked_at__isnull=True).exists())

    def test_recovery_code_is_one_time(self):
        recovery = "recovery-code-once"
        RecoveryCode.objects.create(user=self.user, code_hash=make_password(recovery))
        self.assertEqual(self._password_login().status_code, 200)
        response = self.client.post(
            "/api/auth/totp",
            data=json.dumps({"code": recovery, "recovery": True}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.client.cookies.clear()
        self.assertEqual(self._password_login().status_code, 200)
        response = self.client.post(
            "/api/auth/totp",
            data=json.dumps({"code": recovery, "recovery": True}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)


class GlobalLockIntegrationTests(TestCase):
    def test_competing_node_cannot_take_live_lease(self):
        market = Marketplace.objects.create(slug="test-market", display_name="Test")
        job = Job.objects.create(
            marketplace=market,
            external_id="external-1",
            title="Test job",
            task_class="data",
            reward="10.00",
        )
        first = acquire_job_lock(job.id, node_id="node-a", lease_seconds=300)
        with self.assertRaises(JobLockUnavailable):
            acquire_job_lock(job.id, node_id="node-b", lease_seconds=300)
        release_job_lock(job.id, node_id="node-a", fencing_token=first.fencing_token)
        second = acquire_job_lock(job.id, node_id="node-b", lease_seconds=300)
        self.assertEqual(second.node_id, "node-b")
        self.assertGreaterEqual(second.fencing_token, 1)


class AgentGigsWebhookIntegrationTests(TestCase):
    def test_signed_webhook_is_durable_before_queue_delivery(self):
        market = Marketplace.objects.create(
            slug="agentgigs",
            display_name="AgentGigs",
            status=Marketplace.Status.PAYOUT_BLOCKED,
            enabled=False,
        )
        raw = json.dumps(
            {
                "event": "job.available",
                "timestamp": "2026-08-08T09:00:00Z",
                "data": {"job": {"id": "remote-job-1", "title": "Data cleanup"}},
            },
            separators=(",", ":"),
        ).encode()
        signature = signature_for(raw, "integration-webhook-secret")
        response = self.client.post(
            "/webhooks/agentgigs/",
            data=raw,
            content_type="application/json",
            HTTP_X_AGENTGIGS_SIGNATURE=signature,
        )
        self.assertEqual(response.status_code, 202)
        event = WebhookEvent.objects.get(marketplace=market)
        self.assertEqual(event.event_type, "job.available")
        self.assertEqual(event.external_job_id, "remote-job-1")
        self.assertEqual(event.status, "RECEIVED")

    def test_bad_signature_is_rejected_without_persistence(self):
        Marketplace.objects.create(slug="agentgigs", display_name="AgentGigs")
        response = self.client.post(
            "/webhooks/agentgigs/",
            data=b'{"event":"job.available","data":{"job":{"id":"x"}}}',
            content_type="application/json",
            HTTP_X_AGENTGIGS_SIGNATURE="bad",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(WebhookEvent.objects.count(), 0)
