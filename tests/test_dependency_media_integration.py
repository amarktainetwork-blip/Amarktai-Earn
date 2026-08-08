from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from django.test import TestCase

from control.models import Artifact, Job, JobScore, Marketplace, QAResult
from control.services.dependencies import DependencyRequest, inspect_dependency_request, prepare_dependencies
from planning.models import DependencyPreparation, RepositorySnapshot, WorkPlan
from planning.coding import prepare_coding_plan
from planning.services import execute_work_plan, plan_awarded_job, stage_local_job_asset
from workers.base import WorkRequest
from workers.media.worker import MediaWorker
from workers.registry import operation_spec, registry_manifest


class FakeDependencyBroker:
    def __init__(self):
        self.payload = None

    def prepare(self, payload):
        self.payload = dict(payload)
        return {"cache_key": "amarktai_deps_node_" + "a" * 24, "file_count": 17, "total_bytes": 2048, "cache_hit": False}


class DependencyPreparationIntegrationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repos = self.root / "repos"
        self.repos.mkdir()
        self.market = Marketplace.objects.create(slug="dependency-test", display_name="Dependency Test")
        self.job = Job.objects.create(marketplace=self.market, external_id="dependency-job", title="Fix bug", task_class="Coding", reward="50", state=Job.State.AWARDED)
        self.path = self.repos / str(self.job.id) / ("a" * 40)
        self.path.mkdir(parents=True)
        self.snapshot = RepositorySnapshot.objects.create(
            job=self.job, repository_url="https://github.com/example/repo", owner="example", repository="repo",
            commit_sha="a" * 40, path=str(self.path), status=RepositorySnapshot.Status.VERIFIED,
        )
        self.env = patch.dict(os.environ, {"AMARKTAI_REPO_ROOT": str(self.repos), "DEPENDENCY_PREPARATION_ENABLED": "1"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_python_requires_pins_and_hashes(self):
        digest = "1" * 64
        (self.path / "requirements.txt").write_text(
            f"requests==2.32.5 \\\n    --hash=sha256:{digest}\n",
            encoding="utf-8",
        )
        request, reasons = inspect_dependency_request(self.snapshot)
        self.assertEqual((request.ecosystem, request.manifest_path), ("python", "requirements.txt"))
        self.assertEqual(reasons, [])
        (self.path / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
        request, reasons = inspect_dependency_request(self.snapshot)
        self.assertIsNone(request)
        self.assertIn("PYTHON_REQUIREMENTS_NOT_HASH_LOCKED", reasons)

    def test_oversize_manifest_becomes_a_stable_plan_blocker(self):
        (self.path / "requirements.txt").write_bytes(b"x" * 1025)
        with patch.dict(os.environ, {"DEPENDENCY_MAX_MANIFEST_BYTES": "1024"}, clear=False):
            request, reasons = inspect_dependency_request(self.snapshot)
        self.assertIsNone(request)
        self.assertEqual(reasons, ["DEPENDENCY_MANIFEST_TOO_LARGE"])

    def test_node_lock_v3_is_prepared_and_persisted_without_credentials(self):
        package = json.dumps({"name": "demo", "version": "1.0.0"}, separators=(",", ":")).encode()
        lock = json.dumps({"name": "demo", "version": "1.0.0", "lockfileVersion": 3, "packages": {}}, separators=(",", ":")).encode()
        (self.path / "package.json").write_bytes(package)
        (self.path / "package-lock.json").write_bytes(lock)
        request, reasons = inspect_dependency_request(self.snapshot)
        self.assertEqual(reasons, [])
        self.assertEqual(request.manifest_hash, hashlib.sha256(package + b"\0" + lock).hexdigest())
        broker = FakeDependencyBroker()
        row, cache_key = prepare_dependencies(job=self.job, snapshot=self.snapshot, request=request, broker=broker)
        self.assertEqual(row.status, DependencyPreparation.Status.READY)
        self.assertEqual(cache_key, "amarktai_deps_node_" + "a" * 24)
        self.assertEqual(set(broker.payload), {"snapshot_rel", "ecosystem", "manifest_path", "manifest_hash"})

    def test_unlocked_manifests_fail_closed(self):
        (self.path / "pyproject.toml").write_text("[project]\nname='unsafe'\n", encoding="utf-8")
        request, reasons = inspect_dependency_request(self.snapshot)
        self.assertIsNone(request)
        self.assertIn("PYTHON_LOCKFILE_REQUIRED", reasons)

    def test_coding_planner_carries_verified_dependency_request(self):
        self.job.normalized_payload = {"repository_url": "https://github.com/example/repo", "test_command": "pytest -q", "description": "Fix bug with a small patch"}
        self.job.save(update_fields=["normalized_payload", "updated_at"])
        package = json.dumps({"name": "demo", "version": "1.0.0"}, separators=(",", ":"))
        lock = json.dumps({"name": "demo", "version": "1.0.0", "lockfileVersion": 3, "packages": {}}, separators=(",", ":"))
        (self.path / "package.json").write_text(package, encoding="utf-8")
        (self.path / "package-lock.json").write_text(lock, encoding="utf-8")
        with patch.dict(os.environ, {"SANDBOX_CODING_ENABLED": "1"}, clear=False):
            plan = prepare_coding_plan(self.job.id, stage_repository=False)
        self.assertEqual(plan.status, WorkPlan.Status.READY)
        self.assertEqual(plan.input_spec["dependency_request"]["ecosystem"], "node")
        self.assertEqual(plan.input_spec["dependency_request"]["manifest_path"], "package-lock.json")


class MediaWorkerIntegrationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.jobs = self.root / "jobs"
        self.uploads.mkdir(); self.jobs.mkdir()
        self.env = patch.dict(os.environ, {
            "AMARKTAI_UPLOAD_ROOT": str(self.uploads), "AMARKTAI_JOB_ROOT": str(self.jobs),
            "AMARKTAI_MIN_FREE_DISK_BYTES": "1", "AMARKTAI_MIN_FREE_DISK_PERCENT": "0",
            "AMARKTAI_MIN_MEMORY_HEADROOM_BYTES": "1", "AMARKTAI_LARGE_JOB_MEMORY_HEADROOM_BYTES": "1",
            "AMARKTAI_MAX_LOAD_PER_CPU": "100",
        }, clear=False)
        self.env.start()
        self.market = Marketplace.objects.create(slug="media-test", display_name="Media Test")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _job(self, external_id, title):
        job = Job.objects.create(marketplace=self.market, external_id=external_id, title=title, task_class="Media", reward="20", state=Job.State.AWARDED)
        JobScore.objects.create(job=job, p_acquire="1", p_accept="1", p_payment="1", expected_profit="10", expected_minutes=5)
        return job

    def test_image_resize_runs_full_truth_chain_and_independent_qa(self):
        job = self._job("resize", "Resize image to 40x20")
        source = self.uploads / "source.png"
        Image.new("RGB", (100, 50), "red").save(source)
        stage_local_job_asset(job_id=job.id, path=str(source))
        plan = plan_awarded_job(job.id)
        self.assertEqual((plan.operation, plan.worker_class, plan.status), ("image_resize", "media", WorkPlan.Status.READY))
        with patch("control.services.admission.shutil.which", return_value="/usr/bin/tool"):
            plan = execute_work_plan(plan.id)
        self.assertEqual(plan.status, WorkPlan.Status.QA_PASSED)
        artifact = Artifact.objects.get(job=job)
        with Image.open(artifact.path) as output:
            self.assertEqual(output.size, (40, 20))
        self.assertTrue(QAResult.objects.filter(job=job, check_type="deterministic_media", passed=True).exists())

    def test_ambiguous_media_instruction_blocks(self):
        job = self._job("ambiguous", "Make this image look better")
        source = self.uploads / "ambiguous.png"
        Image.new("RGB", (20, 20), "blue").save(source)
        stage_local_job_asset(job_id=job.id, path=str(source))
        plan = plan_awarded_job(job.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("TRANSFORMATION_NOT_UNAMBIGUOUS", plan.reason_codes)

    def test_decoded_pixel_limit_blocks_decompression_risk(self):
        source = self.uploads / "large.png"
        Image.new("RGB", (50, 50), "green").save(source)
        with patch.dict(os.environ, {"MEDIA_MAX_PIXELS": "1000"}, clear=False):
            result = MediaWorker().execute(WorkRequest(job_id="bounded", workspace=self.jobs / "bounded", inputs={
                "operation": "image_resize", "source": str(source), "width": 10, "height": 10, "output_format": "PNG",
            }))
        self.assertFalse(result.ok)

    def test_ffmpeg_failure_removes_partial_output_and_sets_write_limit(self):
        source = self.uploads / "source.wav"
        source.write_bytes(b"valid-enough-for-mocked-probe")
        workspace = self.jobs / "bounded-ffmpeg"

        def fail_after_partial_write(args, **_kwargs):
            Path(args[-1]).write_bytes(b"x" * 2048)
            return SimpleNamespace(returncode=1, stdout="", stderr="bounded")

        with patch.dict(os.environ, {"MEDIA_MAX_OUTPUT_BYTES": "1024"}, clear=False), \
                patch("workers.media.worker._probe", return_value={"duration_seconds": 1.0}), \
                patch("workers.media.worker.subprocess.run", side_effect=fail_after_partial_write) as runner:
            result = MediaWorker().execute(WorkRequest(job_id="bounded", workspace=workspace, inputs={
                "operation": "media_transcode", "source": str(source), "output_format": "wav",
            }))
        self.assertFalse(result.ok)
        self.assertFalse((workspace / "media-output.wav").exists())
        args = runner.call_args.args[0]
        self.assertEqual(args[args.index("-fs") + 1], "1024")

    def test_registry_exposes_real_media_worker_and_qa(self):
        manifest = {row["worker_class"]: row for row in registry_manifest()}
        self.assertEqual(manifest["media"]["qa_profile"], "media")
        self.assertEqual(manifest["media"]["runtime_commands"], ["ffmpeg", "ffprobe"])
        for operation in ("image_resize", "image_center_crop", "image_convert", "image_compress", "image_thumbnail", "media_trim", "media_transcode", "media_extract_audio"):
            self.assertEqual(operation_spec(operation).worker_class, "media")

    def test_media_planner_requires_explicit_parameters_for_each_operation_family(self):
        cases = (
            ("Resize image to 1200x628", ".png", "image_resize"),
            ("Create a thumbnail 300x300", ".png", "image_thumbnail"),
            ("Centered crop image to 80x60", ".png", "image_center_crop"),
            ("Convert PNG to JPEG quality 85", ".png", "image_convert"),
            ("Compress image as JPEG quality 80", ".png", "image_compress"),
            ("Trim video from 00:00:10 to 00:00:40", ".mp4", "media_trim"),
            ("Extract audio as MP3", ".mp4", "media_extract_audio"),
            ("Transcode video to WebM", ".mp4", "media_transcode"),
        )
        for index, (title, suffix, operation) in enumerate(cases):
            with self.subTest(operation=operation):
                job = self._job(f"planner-{index}", title)
                source = self.uploads / f"planner-{index}{suffix}"
                if suffix == ".png":
                    Image.new("RGB", (1600, 900), "purple").save(source)
                else:
                    source.write_bytes(b"staged-media-placeholder")
                stage_local_job_asset(job_id=job.id, path=str(source))
                self.assertEqual(plan_awarded_job(job.id).operation, operation)
