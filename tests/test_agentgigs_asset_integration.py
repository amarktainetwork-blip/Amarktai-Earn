import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from control.models import Job, Marketplace
from control.services.agentgigs_assets import ingest_agentgigs_assets, sync_awarded_agentgigs_assets
from planning.models import JobAsset, WorkPlan
from planning.services import plan_awarded_job


class FakeAssetAdapter:
    def __init__(self):
        self.nda_calls = 0

    def normalize_job(self, raw):
        return raw

    def ensure_nda(self, job):
        self.nda_calls += 1
        return {"accepted": True}


class AgentGigsAssetIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(slug="agentgigs", display_name="AgentGigs")

    def _job(self, external_id="asset-job-1"):
        return Job.objects.create(
            marketplace=self.market,
            external_id=external_id,
            title="Convert this JSON to CSV",
            task_class="Data Analysis",
            reward="10.00",
            state=Job.State.AWARDED,
            normalized_payload={
                "id": external_id,
                "title": "Convert this JSON to CSV",
                "category": "Data Analysis",
                "budget_max": 1000,
                "description": "Convert the attached JSON file to CSV.",
            },
        )

    def test_message_attachment_is_copied_hashed_verified_and_becomes_plannable(self):
        payload = json.dumps([{"name": "Alice", "age": 30}]).encode()
        adapter = FakeAssetAdapter()
        job = self._job()
        messages = [{
            "id": "message-asset-1",
            "message": "Source attached",
            "attachment_name": "source.json",
            "attachment_url": "https://files.example.test/source.json?signature=short-lived",
            "attachment_size": len(payload),
        }]
        calls = []

        def fetcher(ref, target, maximum):
            calls.append(ref.external_id)
            self.assertLessEqual(len(payload), maximum)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            return len(payload), "application/json"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / "uploads"
            jobs = root / "jobs"
            uploads.mkdir(); jobs.mkdir()
            with patch.dict(os.environ, {
                "AMARKTAI_UPLOAD_ROOT": str(uploads),
                "AMARKTAI_JOB_ROOT": str(jobs),
                "AGENTGIGS_MAX_SOURCE_ASSET_BYTES": "1048576",
            }, clear=False):
                result = ingest_agentgigs_assets(job, adapter, details={"job": {}}, messages=messages, fetcher=fetcher)
                self.assertEqual(result["ingested"], 1)
                asset = JobAsset.objects.get(job=job)
                self.assertEqual(asset.status, JobAsset.Status.VERIFIED)
                self.assertEqual(asset.source, "agentgigs_message_attachment")
                self.assertEqual(asset.mime_type, "application/json")
                self.assertEqual(len(asset.sha256), 64)
                self.assertEqual(asset.url, "")
                self.assertTrue(Path(asset.path).is_file())
                self.assertTrue(str(Path(asset.path).resolve()).startswith(str(uploads.resolve())))
                plan = plan_awarded_job(job.id)
                self.assertEqual(plan.status, WorkPlan.Status.READY)
                self.assertEqual(plan.operation, "json_to_csv")

                # Repeated watcher cycles do not redownload an already verified file.
                result = ingest_agentgigs_assets(
                    job,
                    adapter,
                    details={"job": {}},
                    messages=messages,
                    fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("should not redownload")),
                )
                self.assertEqual(result["existing"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(adapter.nda_calls, 2)

    def test_watcher_asset_stage_discovers_awarded_job_without_manual_staging(self):
        payload = b'[{"name":"Watcher"}]'
        job = self._job("asset-job-watcher")

        class WatcherAdapter(FakeAssetAdapter):
            def get_status(self, opportunity):
                return {"job": {"id": "asset-job-watcher"}}

            def get_messages(self, opportunity):
                return [{
                    "id": "watcher-message-1",
                    "attachment_name": "watcher.json",
                    "attachment_url": "https://files.example.test/watcher.json?sig=temporary",
                    "attachment_size": len(payload),
                }]

        def fetcher(ref, target, maximum):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            return len(payload), "application/json"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / "uploads"
            jobs = root / "jobs"
            uploads.mkdir(); jobs.mkdir()
            with patch.dict(os.environ, {
                "AMARKTAI_UPLOAD_ROOT": str(uploads),
                "AMARKTAI_JOB_ROOT": str(jobs),
                "AGENTGIGS_MAX_ASSET_SYNC_JOBS_PER_CYCLE": "4",
            }, clear=False), patch("control.services.agentgigs_assets._download_signed_asset", side_effect=fetcher):
                result = sync_awarded_agentgigs_assets(WatcherAdapter(), limit=10)

        self.assertEqual(result["jobs"], 1)
        self.assertEqual(result["ingested"], 1)
        asset = JobAsset.objects.get(job=job)
        self.assertEqual(asset.status, JobAsset.Status.VERIFIED)
        self.assertEqual(asset.source, "agentgigs_message_attachment")

    def test_multiple_source_candidates_block_before_any_download(self):
        adapter = FakeAssetAdapter()
        job = self._job("asset-job-multiple")
        messages = [
            {
                "id": "source-one",
                "attachment_name": "one.json",
                "attachment_url": "https://files.example.test/one.json",
                "attachment_size": 10,
            },
            {
                "id": "source-two",
                "attachment_name": "two.csv",
                "attachment_url": "https://files.example.test/two.csv",
                "attachment_size": 10,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / "uploads"
            jobs = root / "jobs"
            uploads.mkdir(); jobs.mkdir()
            with patch.dict(os.environ, {
                "AMARKTAI_UPLOAD_ROOT": str(uploads),
                "AMARKTAI_JOB_ROOT": str(jobs),
            }, clear=False):
                result = ingest_agentgigs_assets(
                    job,
                    adapter,
                    details={"job": {}},
                    messages=messages,
                    fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("multiple sources must not download")),
                )
        self.assertEqual(result["blocked"], 2)
        self.assertEqual(JobAsset.objects.filter(job=job, status=JobAsset.Status.VERIFIED).count(), 0)
        plan = plan_awarded_job(job.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("INPUT_ASSET_NOT_STAGED", plan.reason_codes)

    def test_unsupported_source_is_blocked_without_fetch(self):
        adapter = FakeAssetAdapter()
        job = self._job("asset-job-zip")
        messages = [{
            "id": "zip-1",
            "attachment_name": "archive.zip",
            "attachment_url": "https://files.example.test/archive.zip",
            "attachment_size": 100,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / "uploads"
            jobs = root / "jobs"
            uploads.mkdir(); jobs.mkdir()
            with patch.dict(os.environ, {
                "AMARKTAI_UPLOAD_ROOT": str(uploads),
                "AMARKTAI_JOB_ROOT": str(jobs),
            }, clear=False):
                result = ingest_agentgigs_assets(
                    job,
                    adapter,
                    details={"job": {}},
                    messages=messages,
                    fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("unsupported source must not download")),
                )
        self.assertEqual(result["blocked"], 1)
        asset = JobAsset.objects.get(job=job)
        self.assertEqual(asset.status, JobAsset.Status.BLOCKED)
        plan = plan_awarded_job(job.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("INPUT_ASSET_NOT_STAGED", plan.reason_codes)

    def test_oversized_supported_source_is_blocked_without_fetch(self):
        adapter = FakeAssetAdapter()
        job = self._job("asset-job-big-json")
        messages = [{
            "id": "json-big",
            "attachment_name": "huge.json",
            "attachment_url": "https://files.example.test/huge.json",
            "attachment_size": 2048,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / "uploads"
            jobs = root / "jobs"
            uploads.mkdir(); jobs.mkdir()
            with patch.dict(os.environ, {
                "AMARKTAI_UPLOAD_ROOT": str(uploads),
                "AMARKTAI_JOB_ROOT": str(jobs),
                "AGENTGIGS_MAX_SOURCE_ASSET_BYTES": "1024",
            }, clear=False):
                result = ingest_agentgigs_assets(
                    job,
                    adapter,
                    details={"job": {}},
                    messages=messages,
                    fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("oversized source must not download")),
                )
        self.assertEqual(result["blocked"], 1)
        asset = JobAsset.objects.get(job=job)
        self.assertEqual(asset.status, JobAsset.Status.BLOCKED)
        plan = plan_awarded_job(job.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("INPUT_ASSET_NOT_STAGED", plan.reason_codes)
