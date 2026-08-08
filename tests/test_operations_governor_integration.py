from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from control.models import (
    AdmissionDecision,
    Alert,
    AuditEvent,
    Execution,
    GenXCall,
    Job,
    JobLock,
    JobScore,
    Marketplace,
    RecoveryAction,
    ResourceSnapshot,
    ServiceHeartbeat,
    Submission,
    Worker,
)
from control.ops import nodes_snapshot, storage_snapshot
from control.services.admission import decide_admission
from control.services.recovery import cleanup_storage, heartbeat, recover_persistent_state
from planning.models import WorkPlan


GREEN = {
    "disk_free_bytes": 20 * 1024**3,
    "disk_free_percent": "50.00",
    "memory_available_bytes": 4 * 1024**3,
    "load_per_cpu": "0.20",
    "storage_usage": {"uploads": 0, "jobs": 0, "repositories": 0, "artifacts": 0, "logs": 0, "cache": 0},
    "queue_pressure": {"queued_plans": 0, "active_executions": 0, "code_sandboxes": 0, "genx_jobs": 0, "media_processes": 0},
}


class ResourceGovernorIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(
            slug="governor-market",
            display_name="Governor Market",
            enabled=True,
            status=Marketplace.Status.LIVE,
            payout_ready=True,
            south_africa_verified=True,
        )
        self.job = Job.objects.create(marketplace=self.market, external_id="governor-job", title="Convert JSON", task_class="Data Analysis", reward="20.00", state=Job.State.EXPECTED)
        JobScore.objects.create(job=self.job, p_acquire="1", p_accept="1", p_payment="1", expected_profit="10", expected_profit_per_minute="1", expected_minutes=10, max_genx_credits="2")

    def test_admission_persists_green_and_structured_blockers(self):
        allowed = decide_admission(purpose="ACQUISITION", job=self.job, operation="json_to_csv", metrics=GREEN)
        self.assertTrue(allowed.allowed)
        self.assertTrue(allowed.snapshot.healthy)

        red = dict(GREEN)
        red.update({"disk_free_bytes": 10, "disk_free_percent": "0.10", "memory_available_bytes": 10, "load_per_cpu": "9.0"})
        with patch.dict(os.environ, {"AMARKTAI_MIN_FREE_DISK_BYTES": str(2 * 1024**3), "AMARKTAI_MIN_FREE_DISK_PERCENT": "10", "AMARKTAI_MIN_MEMORY_HEADROOM_BYTES": str(512 * 1024**2), "AMARKTAI_MAX_LOAD_PER_CPU": "1.5"}, clear=False):
            blocked = decide_admission(purpose="ACQUISITION", job=self.job, operation="json_to_csv", metrics=red)
        self.assertFalse(blocked.allowed)
        self.assertIn("DISK_FREE_BYTES_CRITICAL", blocked.reason_codes)
        self.assertIn("MEMORY_HEADROOM_LOW", blocked.reason_codes)
        self.assertIn("CPU_LOAD_HIGH", blocked.reason_codes)
        self.assertTrue(Alert.objects.filter(alert_type="ADMISSION_BLOCKED").exists())
        self.assertEqual(AdmissionDecision.objects.count(), 2)
        self.assertEqual(ResourceSnapshot.objects.count(), 2)

    def test_capability_budget_and_concurrency_fail_closed(self):
        pressure = {**GREEN, "queue_pressure": {**GREEN["queue_pressure"], "code_sandboxes": 1}}
        with patch.dict(os.environ, {"SANDBOX_CODING_ENABLED": "1"}, clear=False):
            blocked = decide_admission(purpose="SANDBOX", job=self.job, operation="code_change_small", metrics=pressure)
        self.assertFalse(blocked.allowed)
        self.assertIn("CODE_SANDBOX_LIMIT", blocked.reason_codes)

        self.job.jobscore.max_genx_credits = 0
        self.job.jobscore.save(update_fields=["max_genx_credits", "updated_at"])
        blocked = decide_admission(purpose="GENX", job=self.job, operation="research_report", metrics=GREEN)
        self.assertIn("GENX_JOB_BUDGET_NOT_POSITIVE", blocked.reason_codes)


class WatchdogRecoveryIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(slug="recovery-market", display_name="Recovery Market")
        self.now = timezone.now()

    def test_restart_reconciliation_recovers_local_state_and_blocks_ambiguity(self):
        job = Job.objects.create(marketplace=self.market, external_id="stale-job", title="Stale job", task_class="Data Analysis", reward="10", state=Job.State.EXECUTING)
        worker = Worker.objects.create(id="stale-worker", worker_class="structured_data", status="EXECUTING", current_job=job)
        Worker.objects.filter(pk=worker.pk).update(last_heartbeat=self.now - timedelta(hours=2), updated_at=self.now - timedelta(hours=2))
        execution = Execution.objects.create(job=job, worker=worker, attempt=1, status="EXECUTING", started_at=self.now - timedelta(hours=2))
        Execution.objects.filter(pk=execution.pk).update(updated_at=self.now - timedelta(hours=2))
        plan = WorkPlan.objects.create(job=job, worker_class="structured_data", operation="json_to_csv", status=WorkPlan.Status.EXECUTING, max_repair_attempts=1)
        WorkPlan.objects.filter(pk=plan.pk).update(updated_at=self.now - timedelta(hours=2))
        JobLock.objects.create(job=job, node_id="dead-node", lease_until=self.now - timedelta(minutes=5), fencing_token=9)

        remote_job = Job.objects.create(marketplace=self.market, external_id="remote-job", title="Remote", task_class="Data Analysis", reward="10", state=Job.State.EXECUTING)
        remote_plan = WorkPlan.objects.create(job=remote_job, status=WorkPlan.Status.SUBMITTING)
        WorkPlan.objects.filter(pk=remote_plan.pk).update(updated_at=self.now - timedelta(hours=1))
        submission = Submission.objects.create(job=remote_job, status="SUBMITTING", version=1)
        GenXCall.objects.create(model="unknown-model", status="UNKNOWN_REMOTE_STATE")

        result = recover_persistent_state(now=self.now)
        worker.refresh_from_db(); execution.refresh_from_db(); plan.refresh_from_db(); remote_plan.refresh_from_db(); submission.refresh_from_db()
        self.assertEqual(worker.status, "OFFLINE")
        self.assertEqual(execution.status, "FAILED")
        self.assertEqual(plan.status, WorkPlan.Status.NEEDS_REPAIR)
        self.assertFalse(JobLock.objects.filter(job=job).exists())
        self.assertEqual(remote_plan.status, WorkPlan.Status.SUBMISSION_RECONCILIATION)
        self.assertEqual(submission.status, "UNKNOWN_REMOTE_STATE")
        self.assertGreaterEqual(result["unknown_remote"], 1)
        self.assertTrue(RecoveryAction.objects.filter(reason_code="AMBIGUOUS_EXTERNAL_MUTATION").exists())
        self.assertTrue(AuditEvent.objects.filter(event_type="operations.recovery_action").exists())

    def test_retention_removes_only_expendable_files_and_is_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"; uploads = root / "uploads"; repos = root / "repos"; cache = root / "cache"; logs = root / "logs"
            for path in (jobs, uploads, repos, cache, logs):
                path.mkdir()
            part = uploads / "download.part"; part.write_bytes(b"partial")
            cache_file = cache / "old.cache"; cache_file.write_bytes(b"cache")
            log_file = logs / "old.log"; log_file.write_text("log", encoding="utf-8")
            old = (self.now - timedelta(days=30)).timestamp()
            for path in (part, cache_file, log_file):
                os.utime(path, (old, old))

            job = Job.objects.create(marketplace=self.market, external_id="failed-cleanup", title="Failed", task_class="Data Analysis", reward="1", state=Job.State.FAILED)
            workspace = jobs / str(job.id) / "attempt-1"; workspace.mkdir(parents=True); (workspace / "failed.tmp").write_text("x", encoding="utf-8")
            execution = Execution.objects.create(job=job, attempt=1, status="FAILED", workspace=str(workspace))
            Execution.objects.filter(pk=execution.pk).update(updated_at=self.now - timedelta(days=10))

            env = {"AMARKTAI_JOB_ROOT": str(jobs), "AMARKTAI_UPLOAD_ROOT": str(uploads), "AMARKTAI_REPO_ROOT": str(repos), "AMARKTAI_CACHE_ROOT": str(cache), "AMARKTAI_LOG_ROOT": str(logs)}
            with patch.dict(os.environ, env, clear=False):
                result = cleanup_storage(now=self.now)
            self.assertFalse(part.exists())
            self.assertFalse(workspace.exists())
            self.assertFalse(cache_file.exists())
            self.assertFalse(log_file.exists())
            self.assertEqual(result["failed_workspaces"], 1)
            self.assertTrue(RecoveryAction.objects.filter(action="BOUNDED_RETENTION_CLEANUP").exists())

    def test_service_heartbeats_surface_in_nodes_dashboard(self):
        heartbeat("agentgigs-watcher", details={"ok": True})
        self.assertTrue(ServiceHeartbeat.objects.filter(service="agentgigs-watcher").exists())
        self.assertEqual(nodes_snapshot()["secondary_rows"][0]["service"], "agentgigs-watcher")
        decide_admission(purpose="WATCHDOG", metrics=GREEN)
        self.assertTrue(storage_snapshot()["meta"]["healthy"])
