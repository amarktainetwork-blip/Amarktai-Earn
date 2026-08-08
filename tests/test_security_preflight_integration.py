from __future__ import annotations

import json
import os
from unittest.mock import patch

import pyotp
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from control.models import (
    AcquisitionPreflight,
    AuthThrottle,
    Job,
    JobScore,
    MarketPolicyVersion,
    Marketplace,
    OwnerSecurityProfile,
    RefreshSession,
)
from control.secrets import encrypt_secret
from control.services.acquisition_preflight import run_acquisition_preflight
from control.services.autonomy import AutonomyMode, acquisition_autonomy, current_mode
from control.services.workload_policy import evaluate_job


GREEN = {
    "disk_free_bytes": 20 * 1024**3,
    "disk_free_percent": "50",
    "memory_available_bytes": 4 * 1024**3,
    "load_per_cpu": "0.1",
    "storage_usage": {"uploads": 0, "jobs": 0, "repositories": 0, "artifacts": 0, "logs": 0, "cache": 0},
    "queue_pressure": {"queued_plans": 0, "active_executions": 0, "code_sandboxes": 0, "genx_jobs": 0, "media_processes": 0},
}


class AuthSecurityIntegrationTests(TestCase):
    def setUp(self):
        self.password = "Strong-Owner-Password-2026!"
        self.user = get_user_model().objects.create_user(username="owner", password=self.password, is_staff=True)
        self.secret = pyotp.random_base32()
        OwnerSecurityProfile.objects.create(
            user=self.user,
            totp_secret_encrypted=encrypt_secret(self.secret),
            totp_confirmed_at=timezone.now(),
        )
        self.client = Client(enforce_csrf_checks=False)

    def _post(self, path, body):
        return self.client.post(path, data=json.dumps(body), content_type="application/json")

    def _login(self):
        self.assertEqual(self._post("/api/auth/login", {"username": "owner", "password": self.password}).status_code, 200)
        self.assertEqual(self._post("/api/auth/totp", {"code": pyotp.TOTP(self.secret).now()}).status_code, 200)

    def test_password_failures_cool_down_without_permanent_lockout(self):
        env = {"AUTH_PASSWORD_IP_MAX_ATTEMPTS": "2", "AUTH_PASSWORD_USER_MAX_ATTEMPTS": "2"}
        with patch.dict(os.environ, env, clear=False):
            for _ in range(3):
                response = self._post("/api/auth/login", {"username": "owner", "password": "wrong"})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"error": "authentication_failed"})
        self.assertTrue(AuthThrottle.objects.filter(locked_until__isnull=False).exists())

    def test_reauthentication_grant_is_single_use_and_security_reset_revokes_sessions(self):
        self._login()
        self.assertTrue(RefreshSession.objects.filter(revoked_at__isnull=True).exists())
        response = self._post("/api/security/reauth", {"password": self.password, "code": pyotp.TOTP(self.secret).now()})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.cookies["amarktai_reauth"]["httponly"])
        reset = self._post("/api/security/reset", {})
        self.assertEqual(reset.status_code, 200)
        self.assertFalse(RefreshSession.objects.filter(revoked_at__isnull=True).exists())
        self.assertEqual(self._post("/api/security/reset", {}).status_code, 401)


class AcquisitionPreflightIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(
            slug="agentgigs", display_name="AgentGigs", enabled=True, status=Marketplace.Status.LIVE,
            payout_ready=True, south_africa_verified=True, fee_rate="0.10",
        )
        MarketPolicyVersion.objects.create(
            marketplace=self.market, policy_hash="approved-v1", automation_allowed=True, webdock_compatible=True,
        )

    def _job(self, title="Convert customer JSON to CSV", payload=None):
        job = Job.objects.create(
            marketplace=self.market, external_id=f"job-{Job.objects.count()}", title=title,
            task_class="Data Analysis", reward="20", state=Job.State.EXPECTED,
            normalized_payload=payload or {"operation": "json_to_csv", "source_filename": "input.json"},
        )
        JobScore.objects.create(
            job=job, p_acquire="0.9", p_accept="0.95", p_payment="0.98", expected_genx_cost="0",
            expected_profit="10", expected_profit_per_minute="1", expected_minutes=10, recommended_offer="20",
        )
        return job

    def test_canonical_modes_and_invalid_mode_fail_closed(self):
        for value, mode, allowed in (("OFF", AutonomyMode.OFF, False), ("SHADOW", AutonomyMode.SHADOW, False), ("LOW_RISK", AutonomyMode.LOW_RISK, True), ("FULL", AutonomyMode.FULL, True), ("typo", AutonomyMode.OFF, False)):
            with self.subTest(value=value), patch.dict(os.environ, {"AUTONOMOUS_MODE": value}, clear=False):
                self.assertEqual(current_mode(), mode)
                self.assertEqual(acquisition_autonomy(switch_enabled=True).may_acquire, allowed)

    def test_all_gates_allow_exact_supported_profitable_operation(self):
        job = self._job()
        env = {"AUTONOMOUS_MODE": "LOW_RISK", "AGENTGIGS_AUTO_APPLY_ENABLED": "1"}
        with patch.dict(os.environ, env, clear=False), patch("control.services.acquisition_preflight.decide_admission") as admission:
            admission.return_value = type("Admission", (), {"allowed": True, "reason_codes": [], "id": "green"})()
            result = run_acquisition_preflight(job)
        self.assertTrue(result.allowed)
        self.assertEqual(result.operation, "json_to_csv")
        self.assertEqual(result.worker_class, "structured_data")
        self.assertTrue(AcquisitionPreflight.objects.filter(job=job, allowed=True).exists())

    def test_shadow_evaluates_but_never_acquires(self):
        job = self._job()
        env = {"AUTONOMOUS_MODE": "SHADOW", "AGENTGIGS_AUTO_APPLY_ENABLED": "1"}
        with patch.dict(os.environ, env, clear=False), patch("control.services.acquisition_preflight.decide_admission") as admission:
            admission.return_value = type("Admission", (), {"allowed": True, "reason_codes": [], "id": "green"})()
            result = run_acquisition_preflight(job)
        self.assertTrue(result.eligible)
        self.assertFalse(result.allowed)
        self.assertIn("AUTONOMY_SHADOW_ONLY", result.reason_codes)

    def test_unknown_and_prohibited_workloads_fail_closed(self):
        unknown = self._job("Do a vague task", {"description": "Help with some data"})
        prohibited = self._job("Run a crypto mining pool", {"operation": "json_to_csv", "source_filename": "input.json"})
        self.assertFalse(evaluate_job(prohibited).allowed)
        env = {"AUTONOMOUS_MODE": "FULL", "AGENTGIGS_AUTO_APPLY_ENABLED": "1"}
        with patch.dict(os.environ, env, clear=False), patch("control.services.acquisition_preflight.decide_admission") as admission:
            admission.return_value = type("Admission", (), {"allowed": True, "reason_codes": [], "id": "green"})()
            unknown_result = run_acquisition_preflight(unknown)
            prohibited_result = run_acquisition_preflight(prohibited)
        self.assertFalse(unknown_result.allowed)
        self.assertIn("OPERATION_NOT_UNAMBIGUOUS", unknown_result.reason_codes)
        self.assertFalse(prohibited_result.allowed)
        self.assertIn("PROHIBITED_CRYPTO_MINING", prohibited_result.reason_codes)

    def test_every_v1_prohibited_infrastructure_category_is_blocked(self):
        cases = {
            "cryptocurrency mining": "PROHIBITED_CRYPTO_MINING",
            "operate a DePIN node": "PROHIBITED_DEPIN",
            "residential proxy resale": "PROHIBITED_BANDWIDTH_RESALE",
            "unauthorized network scanning": "PROHIBITED_UNAUTHORIZED_SCANNING",
            "run a spam campaign": "PROHIBITED_SPAM",
            "create a fake identity": "PROHIBITED_FAKE_IDENTITY",
            "enable fraud using stolen cards": "PROHIBITED_FRAUD",
            "host an LLM locally": "PROHIBITED_LOCAL_INFERENCE",
            "uncontrolled browser automation": "PROHIBITED_BROWSER_AUTOMATION",
        }
        for title, reason in cases.items():
            with self.subTest(reason=reason):
                decision = evaluate_job(self._job(title))
                self.assertFalse(decision.allowed)
                self.assertIn(reason, decision.reason_codes)

    def test_explicit_market_policy_prohibition_blocks_execution_policy(self):
        job = self._job()
        policy = MarketPolicyVersion.objects.get(marketplace=self.market)
        policy.automation_allowed = False
        policy.save(update_fields=["automation_allowed", "updated_at"])
        decision = evaluate_job(job)
        self.assertFalse(decision.allowed)
        self.assertIn("PROHIBITED_MARKET_POLICY", decision.reason_codes)
