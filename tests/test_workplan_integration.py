import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from control.models import Execution, Job, Marketplace, Submission
from planning.models import WorkPlan
from planning.services import execute_work_plan, plan_awarded_job, reconcile_submission_plans, stage_local_job_asset


class WorkPlanIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(slug="integration-market", display_name="Integration Market")

    def test_awarded_json_to_csv_plans_executes_and_passes_independent_qa(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / "uploads"
            jobs = root / "jobs"
            uploads.mkdir(); jobs.mkdir()
            source = uploads / "input.json"
            source.write_text(json.dumps([{"name": " Alice ", "age": 30}, {"name": "Bob", "age": 31}]), encoding="utf-8")
            job = Job.objects.create(
                marketplace=self.market,
                external_id="json-csv-1",
                title="Convert this JSON to CSV",
                task_class="Data Analysis",
                reward="10.00",
                state=Job.State.AWARDED,
                normalized_payload={"description": "Convert the provided JSON file to CSV."},
            )
            with patch.dict(os.environ, {"AMARKTAI_UPLOAD_ROOT": str(uploads), "AMARKTAI_JOB_ROOT": str(jobs)}, clear=False):
                asset = stage_local_job_asset(job_id=job.id, path=str(source), source="integration")
                self.assertEqual(asset.status, "VERIFIED")
                plan = plan_awarded_job(job.id)
                self.assertEqual(plan.status, WorkPlan.Status.READY)
                self.assertEqual(plan.worker_class, "structured_data")
                self.assertEqual(plan.operation, "json_to_csv")
                plan = execute_work_plan(plan.id)
            self.assertEqual(plan.status, WorkPlan.Status.QA_PASSED)
            execution = Execution.objects.get(job=job)
            self.assertEqual(execution.status, "QA_PASSED")
            self.assertTrue((Path(execution.workspace) / "output.csv").exists())

    def test_missing_asset_and_ambiguous_instruction_block_without_execution(self):
        job = Job.objects.create(
            marketplace=self.market,
            external_id="blocked-1",
            title="Analyze this dataset",
            task_class="Data Analysis",
            reward="10.00",
            state=Job.State.AWARDED,
        )
        plan = plan_awarded_job(job.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("INPUT_ASSET_NOT_STAGED", plan.reason_codes)
        self.assertFalse(Execution.objects.filter(job=job).exists())

    def test_submission_reconciliation_closes_plan_after_remote_sync(self):
        market = Marketplace.objects.create(slug="agentgigs", display_name="AgentGigs")
        job = Job.objects.create(
            marketplace=market,
            external_id="reconcile-1",
            title="Submitted job",
            task_class="Data Analysis",
            reward="10.00",
            state=Job.State.SUBMITTED,
        )
        plan = WorkPlan.objects.create(
            job=job,
            status=WorkPlan.Status.SUBMISSION_RECONCILIATION,
            worker_class="structured_data",
            operation="json_to_csv",
        )
        Submission.objects.create(job=job, version=1, status="SUBMITTED", remote_id="remote-submission-1")
        self.assertEqual(reconcile_submission_plans(marketplace_slug="agentgigs"), 1)
        plan.refresh_from_db()
        self.assertEqual(plan.status, WorkPlan.Status.SUBMITTED)
        self.assertIsNotNone(plan.submitted_at)

    def test_repair_limit_blocks_before_an_unbounded_retry(self):
        job = Job.objects.create(
            marketplace=self.market,
            external_id="repair-bound-1",
            title="Convert JSON to CSV",
            task_class="Data Analysis",
            reward="10.00",
            state=Job.State.EXECUTING,
        )
        plan = WorkPlan.objects.create(
            job=job,
            worker_class="structured_data",
            operation="json_to_csv",
            input_spec={"operation": "json_to_csv", "source": "/not/used"},
            status=WorkPlan.Status.NEEDS_REPAIR,
            repair_attempts=1,
            max_repair_attempts=1,
        )
        plan = execute_work_plan(plan.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("MAX_REPAIR_ATTEMPTS_REACHED", plan.reason_codes)
        self.assertEqual(plan.execution_attempts, 0)
