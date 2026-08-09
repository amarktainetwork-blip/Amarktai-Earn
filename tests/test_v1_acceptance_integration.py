from __future__ import annotations

import io
import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone

from control.models import OwnerSecurityProfile
from control.services.v1_acceptance import VALID_STATUSES, _owner_state, build_acceptance_report


class V1AcceptanceIntegrationTests(TestCase):
    def setUp(self):
        owner = get_user_model().objects.create_user(username="acceptance-owner", password="strong-acceptance-password", is_staff=True)
        OwnerSecurityProfile.objects.create(user=owner, totp_secret_encrypted="configured", totp_confirmed_at=timezone.now())
        self.redis_client = Mock()
        self.redis_client.ping.return_value = True

    def test_report_is_machine_readable_and_never_promotes_external_proof(self):
        with patch("control.services.v1_acceptance.redis.Redis.from_url", return_value=self.redis_client):
            report = build_acceptance_report(ci_proven=True)
        self.assertEqual(report["schema_version"], 2)
        self.assertTrue(report["criteria"])
        self.assertFalse([row for row in report["criteria"] if row["status"] not in VALID_STATUSES])
        self.assertFalse([row for row in report["criteria"] if row["status"] == "FAIL"])
        by_id = {row["id"]: row for row in report["criteria"]}
        for identifier in ("public_https", "actual_reboot", "live_genx", "live_market_account", "live_opportunity", "settled_cash"):
            self.assertEqual(by_id[identifier]["status"], "EXTERNAL_PROOF_REQUIRED")
            self.assertTrue(by_id[identifier]["operator_action"])
        self.assertEqual(by_id["owner_login"]["status"], "PASS")
        self.assertEqual(by_id["sandbox_isolation"]["status"], "PASS")
        self.assertEqual(by_id["media_runtime"]["status"], "PASS")
        self.assertEqual(by_id["uncapped_profit_governor"]["status"], "PASS")
        self.assertEqual(by_id["genx_async_session_truth"]["status"], "PASS")
        for identifier in (
            "two_sided_revenue_engine", "service_offering_truth", "inbound_order_uses_canonical_job_lifecycle",
            "global_portfolio_ranking", "seller_pricing_profit_floor", "crypto_markets_offhost_only",
            "nevermined_fiat_only_on_webdock", "skyfire_noncrypto_gate", "hyrve_fail_closed_without_contract",
            "service_capability_requires_execution_proof", "coding_service_blocked_when_sandbox_off",
            "public_web_service_blocked_when_web_disabled", "market_policy_staleness_blocks_mutation",
            "only_settled_is_cash",
        ):
            self.assertEqual(by_id[identifier]["status"], "PASS", identifier)

    def test_non_ci_invocation_keeps_ci_only_claims_blocked(self):
        with patch("control.services.v1_acceptance.redis.Redis.from_url", return_value=self.redis_client):
            report = build_acceptance_report(ci_proven=False)
        by_id = {row["id"]: row for row in report["criteria"]}
        self.assertEqual(by_id["jwt_replay"]["status"], "BLOCKED")
        self.assertEqual(by_id["worker_execution"]["status"], "BLOCKED")
        self.assertFalse(report["ci_proven_context"])

    def test_management_command_emits_parseable_json_without_failing_on_external_items(self):
        output = io.StringIO()
        with patch("control.services.v1_acceptance.redis.Redis.from_url", return_value=self.redis_client):
            call_command("v1_acceptance", "--format", "json", stdout=output)
        report = json.loads(output.getvalue())
        self.assertIn(report["overall_status"], VALID_STATUSES)
        self.assertGreater(report["counts"]["EXTERNAL_PROOF_REQUIRED"], 0)

    def test_missing_runtime_tables_are_blocked_instead_of_crashing(self):
        with patch("control.services.v1_acceptance.OwnerSecurityProfile.objects.filter", side_effect=DatabaseError("missing")):
            result = _owner_state()
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("migrations", result.operator_action.casefold())

    def test_owner_requires_usable_password_and_configured_totp_secret(self):
        OwnerSecurityProfile.objects.all().delete()
        get_user_model().objects.all().delete()
        owner = get_user_model().objects.create_user(username="incomplete-owner", is_staff=True)
        owner.set_unusable_password()
        owner.save(update_fields=["password"])
        OwnerSecurityProfile.objects.create(user=owner, totp_secret_encrypted="", totp_confirmed_at=timezone.now())
        result = _owner_state()
        self.assertEqual(result.status, "BLOCKED")
        self.assertIn("usable password", result.evidence)
