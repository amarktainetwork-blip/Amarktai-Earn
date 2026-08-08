from django.contrib.auth import get_user_model
from django.test import TestCase

from control.jwt_auth import issue_access
from control.models import Artifact, Execution, GenXCall, Job, Marketplace, OwnerSecurityProfile, QAResult, SystemSetting, Worker
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

    def test_sensitive_settings_are_never_returned(self):
        SystemSetting.objects.create(key="secret-example", value={"token": "must-not-leak"}, sensitive=True)
        settings = snapshot("settings", owner=self.user)
        row = next(item for item in settings["rows"] if item["key"] == "secret-example")
        self.assertEqual(row["value"], "CONFIGURED — HIDDEN")
        self.assertNotIn("must-not-leak", str(settings))

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
