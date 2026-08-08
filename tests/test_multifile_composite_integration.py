from __future__ import annotations

import json
import os
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from control.models import Artifact, Execution, Job, Marketplace, QAResult, Worker
from control.services.recovery import recover_persistent_state
from planning.models import JobAsset, JobAssetManifest, WorkPlan, WorkPlanStep
from planning.services import PlanningError, _composite_inputs, execute_work_plan, plan_awarded_job, stage_local_job_asset


class MultiFileCompositeIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(slug="composite-market", display_name="Composite Market")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.jobs = self.root / "jobs"
        self.uploads.mkdir(); self.jobs.mkdir()
        self.env = patch.dict(os.environ, {
            "AMARKTAI_UPLOAD_ROOT": str(self.uploads),
            "AMARKTAI_JOB_ROOT": str(self.jobs),
            "AMARKTAI_MIN_FREE_DISK_BYTES": "1",
            "AMARKTAI_MIN_FREE_DISK_PERCENT": "0",
            "AMARKTAI_MIN_MEMORY_HEADROOM_BYTES": "1",
            "AMARKTAI_MAX_LOAD_PER_CPU": "100",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _job(self, external_id: str, payload=None):
        return Job.objects.create(
            marketplace=self.market,
            external_id=external_id,
            title="Convert and normalize supplied data",
            task_class="Data Analysis",
            reward="10",
            state=Job.State.AWARDED,
            normalized_payload=payload or {},
        )

    def test_verified_manifest_tracks_roles_hashes_and_total_bounds(self):
        job = self._job("manifest")
        brief = self.uploads / "brief.txt"
        data = self.uploads / "data.json"
        brief.write_text("Convert the supplied customer records.", encoding="utf-8")
        data.write_text(json.dumps([{"name": "Alice"}]), encoding="utf-8")
        first = stage_local_job_asset(job_id=job.id, path=str(brief), semantic_role="brief")
        second = stage_local_job_asset(job_id=job.id, path=str(data), semantic_role="expected-output-reference")
        manifest = JobAssetManifest.objects.get(job=job)
        self.assertEqual(manifest.status, JobAssetManifest.Status.VERIFIED)
        self.assertEqual(manifest.file_count, 2)
        self.assertEqual(set(manifest.roles), {"brief", "expected_output_reference"})
        self.assertEqual(len(manifest.manifest_sha256), 64)
        self.assertEqual(first.detected_mime_type, "text/plain")
        self.assertEqual(second.detected_mime_type, "application/json")

        extra = self.uploads / "extra.txt"
        extra.write_text("extra", encoding="utf-8")
        with patch.dict(os.environ, {"JOB_ASSET_MAX_FILES": "2"}, clear=False):
            stage_local_job_asset(job_id=job.id, path=str(extra), semantic_role="reference")
        manifest.refresh_from_db()
        self.assertEqual(manifest.status, JobAssetManifest.Status.BLOCKED)
        self.assertIn("ASSET_FILE_COUNT_LIMIT", manifest.reason_codes)

    def test_mime_mismatch_and_duplicate_content_fail_closed(self):
        job = self._job("asset-safety")
        fake_pdf = self.uploads / "fake.pdf"
        fake_pdf.write_text("not a pdf", encoding="utf-8")
        with self.assertRaisesMessage(PlanningError, "ASSET_MIME_EXTENSION_MISMATCH"):
            stage_local_job_asset(job_id=job.id, path=str(fake_pdf))

        first = self.uploads / "one.txt"
        second = self.uploads / "two.txt"
        first.write_text("same", encoding="utf-8")
        second.write_text("same", encoding="utf-8")
        original = stage_local_job_asset(job_id=job.id, path=str(first), semantic_role="source")
        duplicate = stage_local_job_asset(job_id=job.id, path=str(second), semantic_role="reference")
        self.assertEqual(duplicate.status, JobAsset.Status.BLOCKED)
        self.assertEqual(duplicate.duplicate_of, original)
        self.assertIn("ASSET_DUPLICATE", duplicate.metadata["reason_codes"])

    def test_office_archives_are_inspected_and_active_content_is_blocked(self):
        job = self._job("office-safety")
        safe = self.uploads / "safe.xlsx"
        with zipfile.ZipFile(safe, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/workbook.xml", "<workbook/>")
        asset = stage_local_job_asset(job_id=job.id, path=str(safe), semantic_role="data")
        self.assertTrue(asset.archive_inspected)
        self.assertEqual(asset.detected_mime_type, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        unsafe = self.uploads / "unsafe.xlsx"
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/workbook.xml", "<workbook/>")
            archive.writestr("xl/vbaProject.bin", b"macro")
        with self.assertRaisesMessage(PlanningError, "ASSET_ACTIVE_CONTENT_BLOCKED"):
            stage_local_job_asset(job_id=job.id, path=str(unsafe), semantic_role="data")

    def test_explicit_dag_executes_in_order_and_gates_downstream_on_qa(self):
        workflow = [
            {"key": "convert", "operation": "json_to_csv", "input_asset_roles": ["data"]},
            {"key": "normalize", "operation": "csv_normalize", "depends_on": ["convert"]},
        ]
        job = self._job("dag", {"workflow_steps": workflow})
        source = self.uploads / "records.json"
        source.write_text(json.dumps([{"name": " Alice ", "age": 30}]), encoding="utf-8")
        stage_local_job_asset(job_id=job.id, path=str(source), semantic_role="data")
        plan = plan_awarded_job(job.id)
        self.assertTrue(plan.is_composite)
        self.assertEqual(plan.status, WorkPlan.Status.READY)
        convert, normalize = list(plan.steps.order_by("sequence"))
        self.assertEqual(convert.status, WorkPlanStep.Status.READY)
        self.assertEqual(normalize.status, WorkPlanStep.Status.BLOCKED)
        with self.assertRaisesMessage(PlanningError, "QA-passed upstream"):
            _composite_inputs(normalize)

        with patch("control.services.execution.require_admission"):
            plan = execute_work_plan(plan.id)
        self.assertEqual(plan.status, WorkPlan.Status.QA_PASSED)
        convert.refresh_from_db(); normalize.refresh_from_db()
        self.assertEqual(convert.status, WorkPlanStep.Status.QA_PASSED)
        self.assertEqual(normalize.status, WorkPlanStep.Status.QA_PASSED)
        self.assertTrue(convert.qa_result.passed)
        self.assertTrue(normalize.qa_result.passed)
        self.assertGreater(normalize.input_artifacts.count(), 0)
        self.assertGreater(convert.output_artifacts.count(), 0)
        self.assertGreater(normalize.output_artifacts.count(), 0)
        executions = list(Execution.objects.filter(job=job).order_by("attempt"))
        self.assertEqual(len(executions), 2)
        self.assertEqual(QAResult.objects.filter(job=job, passed=True).count(), 2)
        self.assertTrue(Artifact.objects.filter(job=job, execution=executions[-1]).exists())

    def test_cycle_and_step_limit_block_without_execution(self):
        cycle = [
            {"key": "one", "operation": "json_to_csv", "depends_on": ["two"], "input_asset_roles": ["data"]},
            {"key": "two", "operation": "csv_normalize", "depends_on": ["one"]},
        ]
        job = self._job("cycle", {"workflow_steps": cycle})
        source = self.uploads / "cycle.json"
        source.write_text("[]", encoding="utf-8")
        stage_local_job_asset(job_id=job.id, path=str(source), semantic_role="data")
        plan = plan_awarded_job(job.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("COMPOSITE_DEPENDENCY_CYCLE", plan.reason_codes)
        self.assertFalse(Execution.objects.filter(job=job).exists())

        steps = [{"key": f"step-{index}", "operation": "research_report"} for index in range(3)]
        limited = self._job("limit", {"workflow_steps": steps})
        with patch.dict(os.environ, {"WORKPLAN_MAX_COMPOSITE_STEPS": "2"}, clear=False):
            plan = plan_awarded_job(limited.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("COMPOSITE_STEP_LIMIT", plan.reason_codes)

    def test_malformed_composite_bounds_and_input_specs_block_cleanly(self):
        workflow = [
            {
                "key": "convert",
                "operation": "json_to_csv",
                "input_asset_roles": ["data"],
                "estimated_cost": "not-a-number",
            },
            {
                "key": "normalize",
                "operation": "csv_normalize",
                "depends_on": ["convert"],
                "input_spec": ["not", "a", "mapping"],
            },
        ]
        job = self._job("malformed-dag", {"workflow_steps": workflow})
        source = self.uploads / "malformed.json"
        source.write_text("[]", encoding="utf-8")
        stage_local_job_asset(job_id=job.id, path=str(source), semantic_role="data")
        plan = plan_awarded_job(job.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("STEP_COST_OR_REPAIR_BOUND_INVALID:convert", plan.reason_codes)
        self.assertIn("STEP_INPUT_SPEC_INVALID:normalize", plan.reason_codes)
        self.assertFalse(plan.steps.exists())
        self.assertFalse(Execution.objects.filter(job=job).exists())

        invalid_shape = self._job("invalid-workflow-shape", {"workflow_steps": {"operation": "json_to_csv"}})
        plan = plan_awarded_job(invalid_shape.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("COMPOSITE_STEPS_INVALID", plan.reason_codes)

    def test_watchdog_recovers_stale_composite_step_to_bounded_repair(self):
        job = self._job("stale-step")
        job.state = Job.State.EXECUTING
        job.save(update_fields=["state", "updated_at"])
        plan = WorkPlan.objects.create(
            job=job, is_composite=True, operation="composite", worker_class="composite",
            status=WorkPlan.Status.EXECUTING, max_steps=2,
        )
        worker = Worker.objects.create(id="stale-composite", worker_class="structured_data", status="EXECUTING", current_job=job)
        execution = Execution.objects.create(job=job, worker=worker, attempt=1, status="EXECUTING", started_at=timezone.now() - timedelta(hours=2))
        step = WorkPlanStep.objects.create(
            plan=plan, key="stale", sequence=1, operation="json_to_csv", worker_class="structured_data",
            execution=execution, status=WorkPlanStep.Status.EXECUTING, max_repair_attempts=1,
        )
        stale = timezone.now() - timedelta(hours=2)
        Execution.objects.filter(pk=execution.pk).update(updated_at=stale)
        WorkPlan.objects.filter(pk=plan.pk).update(updated_at=stale)
        with patch.dict(os.environ, {"WATCHDOG_EXECUTION_STALE_SECONDS": "60", "WATCHDOG_PLAN_STALE_SECONDS": "60"}, clear=False):
            recover_persistent_state(now=timezone.now())
        step.refresh_from_db(); plan.refresh_from_db(); execution.refresh_from_db()
        self.assertEqual(execution.status, "FAILED")
        self.assertEqual(step.status, WorkPlanStep.Status.NEEDS_REPAIR)
        self.assertEqual(plan.status, WorkPlan.Status.NEEDS_REPAIR)
        self.assertTrue(step.repair_history)
