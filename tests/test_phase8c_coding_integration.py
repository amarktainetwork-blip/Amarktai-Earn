from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from control.models import Artifact, GenXCall, GenXModelCatalog, Job, JobScore, Marketplace, QAResult, Worker
from control.ops import agents_snapshot, live_work_snapshot
from control.sandbox_tokens import issue_sandbox_token
from control.services.github_repos import GitHubRepositoryError, _safe_extract_tar, ensure_repository_snapshot
from control.services.sandbox_genx_proxy import SandboxGenXProxyError, proxy_chat_completion, stream_wrapper
from planning.models import RepositorySnapshot, WorkPlan
from planning.coding import prepare_coding_plan
from planning.services import execute_work_plan


class FakeBroker:
    def __init__(self, *, patch_text: str = "", agent_exit: int = 0, test_exit: int = 0, test_log: str = "3 passed"):
        self.patch_text = patch_text
        self.agent_exit = agent_exit
        self.test_exit = test_exit
        self.test_log = test_log
        self.calls = []

    def run(self, payload):
        self.calls.append(dict(payload))
        return {
            "sandbox_id": "sandbox-ci-1",
            "agent": payload["agent"],
            "agent_exit_code": self.agent_exit,
            "test_exit_code": self.test_exit,
            "patch": self.patch_text,
            "agent_log": "agent completed",
            "test_log": self.test_log,
            "limits": {"memory": "1024m", "cpus": "1.5", "pids": 256, "timeout_seconds": 900},
        }


class Phase8CCodingAgentIntegrationTests(TestCase):
    PATCH = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+fixed\n"

    def setUp(self):
        self.market = Marketplace.objects.create(slug="phase8c-market", display_name="Phase 8C Market")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repos = self.root / "repos"
        self.jobs = self.root / "jobs"
        self.uploads = self.root / "uploads"
        self.repos.mkdir(); self.jobs.mkdir(); self.uploads.mkdir()
        self.env = patch.dict(os.environ, {
            "AMARKTAI_REPO_ROOT": str(self.repos),
            "AMARKTAI_JOB_ROOT": str(self.jobs),
            "AMARKTAI_UPLOAD_ROOT": str(self.uploads),
            "SANDBOX_CODING_ENABLED": "1",
            "SANDBOX_TOKEN_SECRET": "t" * 64,
            "SANDBOX_BROKER_SECRET": "b" * 64,
        }, clear=False)
        self.env.start()
        GenXModelCatalog.objects.create(
            model_id="dynamic-coding-ci",
            category="text",
            provider="ci",
            active=True,
            price_hint="0.10000000",
            model_payload={"capabilities": ["code", "coding", "software"]},
        )

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _job(self, external_id: str, title: str, description: str, *, test_command: str | None = "pytest -q"):
        payload = {
            "description": description,
            "repository_url": "https://github.com/example/demo",
        }
        if test_command is not None:
            payload["test_command"] = test_command
        job = Job.objects.create(
            marketplace=self.market,
            external_id=external_id,
            title=title,
            task_class="Coding",
            reward="100.00",
            state=Job.State.AWARDED,
            normalized_payload=payload,
        )
        JobScore.objects.create(
            job=job,
            p_acquire="1.00000",
            p_accept="1.00000",
            p_payment="1.00000",
            expected_profit="80.00",
            expected_minutes=30,
            max_genx_credits="3.0000",
        )
        return job

    def _snapshot(self, job, sha: str = "a" * 40):
        path = self.repos / str(job.id) / sha
        path.mkdir(parents=True)
        (path / "app.py").write_text("old\n", encoding="utf-8")
        return RepositorySnapshot.objects.create(
            job=job,
            provider="github",
            repository_url="https://github.com/example/demo",
            owner="example",
            repository="demo",
            ref="main",
            commit_sha=sha,
            path=str(path),
            file_count=1,
            total_bytes=4,
            status=RepositorySnapshot.Status.VERIFIED,
        )

    def _assert_visible(self, job, worker_class: str):
        self.assertTrue(any(row["worker_class"] == worker_class for row in agents_snapshot()["rows"]))
        row = next(item for item in live_work_snapshot()["rows"] if item["job"] == str(job.id))
        self.assertEqual(row["worker"], worker_class)
        self.assertEqual(row["qa"], "PASS")
        self.assertGreaterEqual(row["artifacts"], 1)

    def test_small_code_routes_to_aider_executes_patch_qa_and_dashboard(self):
        job = self._job("small-1", "Fix bug in validation", "Fix bug with a small patch and keep behavior compatible.")
        self._snapshot(job)
        plan = prepare_coding_plan(job.id)
        self.assertEqual((plan.worker_class, plan.operation, plan.status), ("code_small", "code_change_small", WorkPlan.Status.READY))
        broker = FakeBroker(patch_text=self.PATCH)
        with patch("workers.coding.common.configured_broker", return_value=broker):
            plan = execute_work_plan(plan.id)
        self.assertEqual(plan.status, WorkPlan.Status.QA_PASSED)
        self.assertEqual(broker.calls[0]["agent"], "aider")
        self.assertTrue(Artifact.objects.filter(job=job, path__endswith="changes.patch.txt").exists())
        self.assertTrue(QAResult.objects.filter(job=job, check_type="sandbox_code_patch", passed=True).exists())
        self._assert_visible(job, "code_small")

    def test_heavy_code_routes_to_openhands_and_requires_independent_tests(self):
        job = self._job("heavy-1", "Refactor multiple files", "Refactor multiple files and implement feature behavior safely.")
        self._snapshot(job, "b" * 40)
        plan = prepare_coding_plan(job.id)
        self.assertEqual((plan.worker_class, plan.operation), ("code_heavy", "code_change_heavy"))
        broker = FakeBroker(patch_text=self.PATCH, test_exit=0)
        with patch("workers.coding.common.configured_broker", return_value=broker):
            plan = execute_work_plan(plan.id)
        self.assertEqual(plan.status, WorkPlan.Status.QA_PASSED)
        self.assertEqual(broker.calls[0]["agent"], "openhands")
        self._assert_visible(job, "code_heavy")

        failed = self._job("heavy-2", "Refactor multiple files", "Refactor multiple files and implement feature behavior safely.")
        self._snapshot(failed, "c" * 40)
        failed_plan = prepare_coding_plan(failed.id)
        with patch("workers.coding.common.configured_broker", return_value=FakeBroker(patch_text=self.PATCH, test_exit=1, test_log="1 failed")):
            failed_plan = execute_work_plan(failed_plan.id)
        self.assertEqual(failed_plan.status, WorkPlan.Status.NEEDS_REPAIR)
        self.assertTrue(QAResult.objects.filter(job=failed, check_type="sandbox_code_patch", passed=False).exists())

    def test_ci_testing_runs_without_genx_token_and_uses_test_qa(self):
        job = self._job("ci-1", "Run the tests", "Run the test suite and report whether it passes.")
        self._snapshot(job, "d" * 40)
        plan = prepare_coding_plan(job.id)
        self.assertEqual((plan.worker_class, plan.operation), ("ci_testing", "run_repository_tests"))
        broker = FakeBroker(test_log="12 passed")
        with patch("workers.coding.common.configured_broker", return_value=broker):
            plan = execute_work_plan(plan.id)
        self.assertEqual(plan.status, WorkPlan.Status.QA_PASSED)
        self.assertEqual(broker.calls[0]["agent"], "ci")
        self.assertNotIn("scoped_token", broker.calls[0])
        self.assertTrue(QAResult.objects.filter(job=job, check_type="sandbox_ci", passed=True).exists())
        self._assert_visible(job, "ci_testing")

    def test_planner_blocks_without_explicit_tests_and_when_sandbox_disabled(self):
        missing = self._job("block-1", "Fix bug in parser", "Fix bug in parser with a small patch.", test_command=None)
        self._snapshot(missing, "e" * 40)
        plan = prepare_coding_plan(missing.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("TEST_COMMAND_NOT_EXPLICIT", plan.reason_codes)

        disabled = self._job("block-2", "Fix bug in parser", "Fix bug in parser with a small patch.")
        self._snapshot(disabled, "f" * 40)
        with patch.dict(os.environ, {"SANDBOX_CODING_ENABLED": "0"}, clear=False):
            plan = prepare_coding_plan(disabled.id)
        self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("SANDBOX_CODING_DISABLED", plan.reason_codes)


class FakeCache:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value


class FakeHTTPResponse:
    def __init__(self, payload, status_code=200, headers=None, chunks=None):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = headers or {}
        self._chunks = chunks or []

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1024 * 1024):
        yield from self._chunks


class FakeProxySession:
    def __init__(self, response):
        self.response = response
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response


class Phase8CGenXProxyIntegrationTests(TestCase):
    def test_scoped_proxy_accounts_usage_caches_replay_and_preserves_tool_calls(self):
        market = Marketplace.objects.create(slug="phase8c-proxy", display_name="Phase 8C Proxy")
        job = Job.objects.create(marketplace=market, external_id="proxy-1", title="Fix code", task_class="Coding", reward="40.00", state=Job.State.AWARDED)
        JobScore.objects.create(job=job, p_acquire="1.00000", p_accept="1.00000", p_payment="1.00000", expected_profit="30.00", expected_minutes=15, max_genx_credits="2.0000")
        Worker.objects.create(id="code-small-proxy", worker_class="code_small", version="1.0.0", status="READY")
        with patch.dict(os.environ, {"SANDBOX_TOKEN_SECRET": "s" * 64, "GENX_API_KEY": "master-genx-key-for-ci", "SANDBOX_LLM_ESTIMATED_CREDITS": "0.25"}, clear=False):
            token = issue_sandbox_token(job_id=str(job.id), worker_id="code-small-proxy", model="coding-model-ci", max_credits=Decimal("1.0"), ttl_seconds=300)
            payload = {
                "id": "chatcmpl-ci",
                "model": "coding-model-ci",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "edit", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}],
                "usage": {"credits": "0.2000"},
            }
            session = FakeProxySession(FakeHTTPResponse(payload))
            cache = FakeCache()
            body = {"model": "openai/coding-model-ci", "messages": [{"role": "user", "content": "Fix it"}], "stream": True}
            response, requested_stream = proxy_chat_completion(token, body, session=session, cache=cache)
            self.assertTrue(requested_stream)
            call = GenXCall.objects.get(job=job)
            self.assertEqual(call.status, "COMPLETED")
            self.assertEqual(call.credits, Decimal("0.2000"))
            self.assertEqual(call.task_class, "coding_sandbox")
            self.assertEqual(len(session.posts), 1)
            wrapped = stream_wrapper(response).decode()
            self.assertIn('"tool_calls"', wrapped)
            self.assertIn("[DONE]", wrapped)

            replay, _ = proxy_chat_completion(token, body, session=session, cache=cache)
            self.assertEqual(replay["id"], "chatcmpl-ci")
            self.assertEqual(len(session.posts), 1)
            self.assertEqual(GenXCall.objects.filter(job=job).count(), 1)

            with self.assertRaises(SandboxGenXProxyError):
                proxy_chat_completion(token, {"model": "other-model", "messages": [{"role": "user", "content": "No"}]}, session=session, cache=cache)


class FakeGitHubSession:
    def __init__(self, archive: bytes):
        self.archive = archive
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url == "https://api.github.com/repos/example/demo":
            return FakeHTTPResponse({"default_branch": "main"})
        if url == "https://api.github.com/repos/example/demo/commits/main":
            return FakeHTTPResponse({"sha": "1" * 40})
        if url == "https://api.github.com/repos/example/demo/tarball/" + "1" * 40:
            return FakeHTTPResponse({}, status_code=302, headers={"Location": "https://codeload.github.com/example/demo/legacy.tar.gz/" + "1" * 40})
        if url.startswith("https://codeload.github.com/"):
            return FakeHTTPResponse({}, chunks=[self.archive])
        raise AssertionError(f"unexpected URL {url}")


def _tar_bytes(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo("demo-root/")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for entry in entries:
            name, data, kind, *mode = entry
            info = tarfile.TarInfo("demo-root/" + name)
            if mode:
                info.mode = mode[0]
            if kind == "file":
                raw = data if isinstance(data, bytes) else str(data).encode()
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = str(data)
                archive.addfile(info)
            else:
                raise AssertionError(kind)
    return buffer.getvalue()


class Phase8CGitHubRepositoryIntegrationTests(TestCase):
    def test_controller_gateway_stages_verified_snapshot_without_forwarding_token_to_archive_host(self):
        market = Marketplace.objects.create(slug="phase8c-github", display_name="Phase 8C GitHub")
        job = Job.objects.create(
            marketplace=market,
            external_id="repo-1",
            title="Fix bug in code",
            task_class="Coding",
            reward="20.00",
            state=Job.State.AWARDED,
            normalized_payload={"repository_url": "https://github.com/example/demo", "test_command": "pytest -q"},
        )
        archive = _tar_bytes([("app.py", b"print('ok')\n", "file")])
        fake = FakeGitHubSession(archive)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"AMARKTAI_REPO_ROOT": tmp, "GITHUB_TOKEN": "controller-only-secret-token"}, clear=False):
            snapshot = ensure_repository_snapshot(job.id, session=fake)
            self.assertEqual(snapshot.status, RepositorySnapshot.Status.VERIFIED)
            self.assertEqual(snapshot.commit_sha, "1" * 40)
            self.assertTrue((Path(snapshot.path) / "app.py").is_file())
            codeload_call = next(call for call in fake.calls if call[1].startswith("https://codeload.github.com/"))
            self.assertNotIn("Authorization", codeload_call[2].get("headers", {}))

    def test_archive_extraction_blocks_symlink_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "safe"
            with self.assertRaises(GitHubRepositoryError):
                _safe_extract_tar(_tar_bytes([("link", "/etc/passwd", "symlink")]), destination, max_files=10, max_unpacked_bytes=1024)

            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                root = tarfile.TarInfo("demo-root/")
                root.type = tarfile.DIRTYPE
                archive.addfile(root)
                raw = b"escape"
                info = tarfile.TarInfo("demo-root/../escape.txt")
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
            with self.assertRaises(GitHubRepositoryError):
                _safe_extract_tar(buffer.getvalue(), destination, max_files=10, max_unpacked_bytes=1024)

    def test_archive_extraction_preserves_only_owner_executable_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "safe"
            archive = _tar_bytes([
                ("scripts/test.sh", b"#!/bin/sh\n", "file", 0o755),
                ("README.md", b"read me\n", "file", 0o644),
            ])
            _safe_extract_tar(archive, destination, max_files=10, max_unpacked_bytes=1024)
            if os.name != "nt":
                self.assertEqual((destination / "scripts" / "test.sh").stat().st_mode & 0o777, 0o700)
                self.assertEqual((destination / "README.md").stat().st_mode & 0o777, 0o600)

    def test_planner_rejects_verified_snapshot_for_changed_repository(self):
        market = Marketplace.objects.create(slug="phase8c-stale", display_name="Phase 8C Stale")
        job = Job.objects.create(
            marketplace=market,
            external_id="repo-stale",
            title="Fix bug in code",
            task_class="Coding",
            reward="20.00",
            state=Job.State.AWARDED,
            normalized_payload={
                "repository_url": "https://github.com/example/new-repo",
                "repository_ref": "release",
                "test_command": "pytest -q",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "snapshot"
            snapshot_path.mkdir()
            RepositorySnapshot.objects.create(
                job=job,
                repository_url="https://github.com/example/old-repo",
                owner="example",
                repository="old-repo",
                ref="main",
                commit_sha="2" * 40,
                path=str(snapshot_path),
                status=RepositorySnapshot.Status.VERIFIED,
            )
            with patch.dict(os.environ, {"SANDBOX_CODING_ENABLED": "1"}, clear=False):
                plan = prepare_coding_plan(job.id, stage_repository=False)
            self.assertEqual(plan.status, WorkPlan.Status.BLOCKED)
            self.assertIn("REPOSITORY_NOT_STAGED", plan.reason_codes)
