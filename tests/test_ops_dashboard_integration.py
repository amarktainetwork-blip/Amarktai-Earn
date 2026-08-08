from django.contrib.auth import get_user_model
from django.test import TestCase
from unittest.mock import patch

from control.jwt_auth import issue_access
from control.models import (
    AcquisitionPreflight, Application, Artifact, Bid, Claim, Execution, GenXCall, GenXModelCatalog, Job, Marketplace,
    MarketPolicyVersion, OwnerSecurityProfile, QAResult, Revision, Submission, SystemSetting, Worker,
)
from control.ops import snapshot
from planning.models import WorkPlan


class OperationsDashboardIntegrationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ops-owner", password="this-is-a-long-test-password", is_staff=True)
        OwnerSecurityProfile.objects.create(user=self.user)
        self.market = Marketplace.objects.create(slug="ops-market", display_name="Ops Market")
        self.job = Job.objects.create(
            marketplace=self.market,
            external_id="ops-job-1",
            title="Convert JSON to CSV",
            task_class="Data Analysis",
            reward="12.00",
            state=Job.State.EXECUTING,
        )
        self.plan = WorkPlan.objects.create(
            job=self.job,
            worker_class="structured_data",
            operation="json_to_csv",
            status=WorkPlan.Status.EXECUTING,
        )
        self.worker = Worker.objects.create(
            id="structured-data-ops",
            worker_class="structured_data",
            version="1.0.0",
            status="EXECUTING",
            current_job=self.job,
        )
        self.execution = Execution.objects.create(
            job=self.job,
            worker=self.worker,
            attempt=1,
            status="EXECUTING",
            workspace="/var/lib/amarktai-earn/jobs/test",
        )
        Artifact.objects.create(job=self.job, execution=self.execution, path="/var/lib/amarktai-earn/jobs/test/output.csv", sha256="a" * 64, size_bytes=10, mime_type="text/csv")
        QAResult.objects.create(job=self.job, execution=self.execution, check_type="deterministic_csv", passed=True, score=1)
        GenXCall.objects.create(
            request_key="ops-genx-1",
            job=self.job,
            worker=self.worker,
            model="dynamic-model",
            task_class="data",
            status="COMPLETED",
            estimated_credits="1",
            max_allowed_credits="2",
            credits="0.5",
        )

    def test_agents_and_live_work_are_backed_by_runtime_records(self):
        agents = snapshot("agents", owner=self.user)
        row = next(item for item in agents["rows"] if item["id"] == self.worker.id)
        self.assertEqual(row["worker_class"], "structured_data")
        self.assertEqual(row["status"], "EXECUTING")
        self.assertEqual(row["current_job"], str(self.job.id))
        self.assertIn("json_to_csv", row["operations"])

        live = snapshot("live-work", owner=self.user)
        job = next(item for item in live["rows"] if item["job"] == str(self.job.id))
        self.assertEqual(job["worker"], "structured_data")
        self.assertEqual(job["execution"], "EXECUTING")
        self.assertEqual(job["plan"], "EXECUTING")
        self.assertEqual(job["qa"], "PASS")
        self.assertEqual(job["artifacts"], 1)
        self.assertEqual(job["operation"], "json_to_csv")
        self.assertEqual(job["repair_attempts"], "0/1")
        self.assertEqual(job["open_revisions"], 0)

    def test_live_work_and_markets_surface_submission_policy_activity_and_blockers(self):
        Submission.objects.create(job=self.job, artifact=Artifact.objects.get(job=self.job), version=1, status="SUBMITTED")
        Revision.objects.create(job=self.job, message="Please revise", status="REQUIRED")
        Application.objects.create(job=self.job, status="UNKNOWN_REMOTE_STATE")
        MarketPolicyVersion.objects.create(
            marketplace=self.market, policy_hash="policy-v1", automation_allowed=False, webdock_compatible=True,
        )
        AcquisitionPreflight.objects.create(
            job=self.job, autonomy_mode="SHADOW", operation="json_to_csv", worker_class="structured_data",
            eligible=True, allowed=False, reason_codes=["AUTONOMY_SHADOW_ONLY"],
        )

        live = snapshot("live-work", owner=self.user)["rows"][0]
        self.assertEqual(live["submission"], "SUBMITTED")
        self.assertEqual(live["submission_version"], 1)
        self.assertEqual(live["open_revisions"], 1)

        market = snapshot("markets", owner=self.user)["rows"][0]
        self.assertFalse(market["policy_automation_allowed"])
        self.assertEqual(market["unknown_remote_state"], 1)
        self.assertEqual(market["latest_preflight"], "BLOCKED")
        self.assertIn("AUTONOMY_SHADOW_ONLY", market["blockers"])
        self.assertIn("PAYOUT_NOT_READY", market["blockers"])

    def test_agents_expose_production_enablement_separately_from_runtime_status(self):
        with patch.dict("os.environ", {"SANDBOX_CODING_ENABLED": "0", "GENX_API_KEY": ""}, clear=False):
            agents = snapshot("agents", owner=self.user)["rows"]
        structured = next(row for row in agents if row["worker_class"] == "structured_data")
        coding = next(row for row in agents if row["worker_class"] == "code_small")
        self.assertTrue(structured["production_enabled"])
        self.assertFalse(coding["production_enabled"])
        self.assertIn("CODING_SANDBOX_DISABLED", coding["enablement_reason_codes"])

    def test_agents_require_sandbox_secrets_and_worker_specific_genx_capability(self):
        GenXModelCatalog.objects.create(
            model_id="translation-specialist",
            category="translation",
            provider="test",
            active=True,
            model_payload={"capabilities": ["translation"]},
        )
        environment = {
            "SANDBOX_CODING_ENABLED": "1",
            "SANDBOX_BROKER_SECRET": "short",
            "SANDBOX_TOKEN_SECRET": "short",
            "GENX_API_KEY": "configured",
        }
        with patch.dict("os.environ", environment, clear=False):
            agents = snapshot("agents", owner=self.user)["rows"]
        localization = next(row for row in agents if row["worker_class"] == "localization")
        transcription = next(row for row in agents if row["worker_class"] == "transcription")
        coding = next(row for row in agents if row["worker_class"] == "code_small")
        ci_testing = next(row for row in agents if row["worker_class"] == "ci_testing")
        self.assertTrue(localization["production_enabled"])
        self.assertFalse(transcription["production_enabled"])
        self.assertIn("GENX_CAPABILITY_UNAVAILABLE", transcription["enablement_reason_codes"])
        self.assertIn("SANDBOX_BROKER_SECRET_INVALID", coding["enablement_reason_codes"])
        self.assertIn("SANDBOX_TOKEN_SECRET_INVALID", coding["enablement_reason_codes"])
        self.assertIn("SANDBOX_BROKER_SECRET_INVALID", ci_testing["enablement_reason_codes"])
        self.assertNotIn("SANDBOX_TOKEN_SECRET_INVALID", ci_testing["enablement_reason_codes"])

    def test_overview_counts_every_ambiguous_external_mutation(self):
        Application.objects.create(job=self.job, status="UNKNOWN_REMOTE_STATE")
        Bid.objects.create(job=self.job, amount="10.00", status="UNKNOWN_REMOTE_STATE")
        Claim.objects.create(job=self.job, status="UNKNOWN_REMOTE_STATE")
        Submission.objects.create(job=self.job, status="UNKNOWN_REMOTE_STATE")
        GenXCall.objects.create(
            request_key="ops-genx-unknown",
            job=self.job,
            worker=self.worker,
            model="dynamic-model",
            task_class="data",
            status="UNKNOWN_REMOTE_STATE",
            estimated_credits="1",
            max_allowed_credits="2",
        )
        overview = snapshot("overview", owner=self.user)
        card = next(row for row in overview["cards"] if row["label"] == "UNKNOWN REMOTE STATE")
        self.assertEqual(card["value"], 5)

    def test_sensitive_settings_are_never_returned(self):
        SystemSetting.objects.create(key="secret-example", value={"token": "must-not-leak"}, sensitive=True)
        settings = snapshot("settings", owner=self.user)
        row = next(item for item in settings["rows"] if item["key"] == "secret-example")
        self.assertEqual(row["value"], "CONFIGURED — HIDDEN")
        self.assertNotIn("must-not-leak", str(settings))

    def test_security_reports_only_configured_hidden_secret_state(self):
        with patch.dict("os.environ", {"GENX_API_KEY": "must-not-leak"}, clear=False):
            security = snapshot("security", owner=self.user)
        self.assertIn("CONFIGURED — HIDDEN", str(security))
        self.assertNotIn("must-not-leak", str(security))

    def test_authenticated_dashboard_api_exposes_registry_and_runtime_truth(self):
        self.client.cookies["amarktai_access"] = issue_access(self.user)
        response = self.client.get("/api/ops/agents")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["section"], "agents")
        self.assertTrue(any(row["id"] == self.worker.id for row in payload["rows"]))

    def test_unauthenticated_dashboard_api_is_rejected(self):
        response = self.client.get("/api/ops/agents")
        self.assertEqual(response.status_code, 401)
