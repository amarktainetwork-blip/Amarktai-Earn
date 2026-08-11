import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from control.sandbox_tokens import SandboxTokenError, issue_sandbox_token, verify_sandbox_token
from markets.agentgigs.client import AgentGigsAdapter
from sandbox_broker.server import _security_args
from workers.qa.runtime import run_qa
from workers.registry import operation_spec, registry_manifest


ROOT = Path(__file__).resolve().parents[1]


class Phase8CCodingContractTests(unittest.TestCase):
    def test_registry_contains_three_coding_phase_agents(self):
        manifest = {row["worker_class"]: row for row in registry_manifest()}
        self.assertTrue(manifest["code_small"]["requires_genx"])
        self.assertTrue(manifest["code_heavy"]["requires_genx"])
        self.assertFalse(manifest["ci_testing"]["requires_genx"])
        self.assertEqual(operation_spec("code_change_small").worker_class, "code_small")
        self.assertEqual(operation_spec("code_change_heavy").worker_class, "code_heavy")
        self.assertEqual(operation_spec("run_repository_tests").worker_class, "ci_testing")

    def test_scoped_sandbox_token_is_signed_expiring_and_model_bound(self):
        with patch.dict(os.environ, {"SANDBOX_TOKEN_SECRET": "x" * 64}, clear=False):
            token = issue_sandbox_token(job_id="job-1", worker_id="worker-1", model="coding-model", max_credits=Decimal("2.5"), ttl_seconds=120)
            claims = verify_sandbox_token(token)
            self.assertEqual(claims.job_id, "job-1")
            self.assertEqual(claims.worker_id, "worker-1")
            self.assertEqual(claims.model, "coding-model")
            self.assertEqual(claims.max_credits, Decimal("2.5"))
            encoded, sig = token.split(".", 1)
            with self.assertRaises(SandboxTokenError):
                replacement = "A" if sig[0] != "A" else "B"
                verify_sandbox_token(encoded + "." + (replacement + sig[1:]))
            with self.assertRaises(SandboxTokenError):
                verify_sandbox_token(token, now=claims.expires_at)

    def test_agent_container_security_contract_has_limits_and_no_socket_mount(self):
        args = _security_args(network="amarktai_sandbox_llm", volume_name="jobvol", memory="1024m", cpus="1.5", pids=256)
        text = " ".join(args)
        self.assertIn("--read-only", args)
        self.assertIn("--cap-drop ALL", text)
        self.assertIn("no-new-privileges:true", text)
        self.assertIn("--pids-limit 256", text)
        self.assertIn("--memory 1024m", text)
        self.assertIn("--cpus 1.5", text)
        self.assertIn("--user 10001:10001", text)
        self.assertNotIn("--cap-add", args)
        self.assertNotIn("/var/run/docker.sock", text)
        broker = (ROOT / "sandbox_broker" / "server.py").read_text(encoding="utf-8")
        seed = broker.split('seed = [', 1)[1].split('        ]', 1)[0]
        self.assertIn('"--cap-add", "DAC_OVERRIDE"', seed)
        self.assertIn('"--cap-add", "CHOWN"', seed)
        self.assertIn("chown 0:0 /workspace", seed)
        self.assertIn("cp -R --no-preserve=ownership,timestamps", seed)
        self.assertIn("chown -R 10001:10001 /workspace", seed)
        self.assertIn("git tag amarktai-baseline", broker)
        self.assertIn("git add -N -- .", broker)
        self.assertIn("git diff --binary --no-ext-diff amarktai-baseline", broker)
        self.assertIn("hmac.compare_digest", broker)

    def test_compose_exposes_docker_socket_only_to_deterministic_broker(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertEqual(compose.count("/var/run/docker.sock:/var/run/docker.sock"), 1)
        broker_section = compose.split("sandbox-broker:", 1)[1].split("sandbox-agent-image:", 1)[0]
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", broker_section)
        watcher_section = compose.split("watcher:", 1)[1].split("genx-proxy:", 1)[0]
        self.assertIn("repo_cache:/var/lib/amarktai-earn/repos", watcher_section)
        self.assertIn("internal: true", compose)
        sandboxfile = (ROOT / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("docker.sock", sandboxfile)

    def test_pinned_agent_image_and_headless_noninteractive_contract(self):
        dockerfile = (ROOT / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
        runner = (ROOT / "sandbox" / "run-agent.sh").read_text(encoding="utf-8")
        self.assertIn("aider-chat==0.86.2", dockerfile)
        self.assertIn("openhands==1.16.0", dockerfile)
        self.assertIn("openhands-constraints.txt", dockerfile)
        constraints = (ROOT / "sandbox" / "openhands-constraints.txt").read_text(encoding="utf-8")
        self.assertIn("opentelemetry-sdk==1.39.1", constraints)
        self.assertIn("opentelemetry-semantic-conventions==0.60b1", constraints)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("--no-stream", runner)
        self.assertIn("--config /dev/null", runner)
        self.assertIn("--env-file /dev/null", runner)
        self.assertIn("RUNTIME=process", runner)
        self.assertIn("--headless --json --override-with-envs", runner)

    def test_code_and_ci_qa_require_independent_test_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patch_file = root / "changes.patch.txt"
            patch_file.write_text("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n+fixed\n", encoding="utf-8")
            outcome = run_qa("code_patch", patch_file, {"agent_exit_code": 0, "test_exit_code": 0, "sandbox_id": "s1"})
            self.assertTrue(outcome.passed)
            failed = run_qa("code_patch", patch_file, {"agent_exit_code": 0, "test_exit_code": 1})
            self.assertFalse(failed.passed)

            report = root / "test-report.txt"
            report.write_text("3 passed", encoding="utf-8")
            self.assertTrue(run_qa("ci", report, {"test_exit_code": 0}).passed)
            self.assertFalse(run_qa("ci", report, {"test_exit_code": 1}).passed)

    def test_code_patch_deliverable_uses_agentgigs_supported_suffix(self):
        for worker_path in ("aider_worker.py", "openhands_worker.py"):
            source = (ROOT / "workers" / "coding" / worker_path).read_text(encoding="utf-8")
            self.assertIn('"changes.patch.txt"', source)
        self.assertIn(".txt", AgentGigsAdapter.ALLOWED_DELIVERABLE_SUFFIXES)

    def test_planner_and_proxy_fail_closed_contracts_are_present(self):
        planner = (ROOT / "planning" / "coding.py").read_text(encoding="utf-8")
        for marker in ("SANDBOX_CODING_DISABLED", "REPOSITORY_NOT_STAGED", "TEST_COMMAND_NOT_EXPLICIT", "code_change_small", "code_change_heavy", "run_repository_tests"):
            self.assertIn(marker, planner)
        proxy = (ROOT / "control" / "services" / "sandbox_genx_proxy.py").read_text(encoding="utf-8")
        self.assertIn("automatic replay is blocked", proxy)
        self.assertIn("requested_model != claims.model", proxy)
        self.assertNotIn('GENX_API_KEY={scoped_token}', proxy)
        self.assertIn('delta["tool_calls"]', proxy)
        self.assertIn('delta["function_call"]', proxy)

    def test_github_gateway_contains_archive_traversal_and_special_file_guards(self):
        source = (ROOT / "control" / "services" / "github_repos.py").read_text(encoding="utf-8")
        for marker in ("issym()", "islnk()", "isdev()", '".." in relative.parts', "GITHUB_REPOSITORY_MAX_ARCHIVE_BYTES", "GITHUB_REPOSITORY_MAX_UNPACKED_BYTES", "_ALLOWED_ARCHIVE_HOSTS"):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
