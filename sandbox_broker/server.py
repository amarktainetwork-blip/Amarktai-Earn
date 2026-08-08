from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class BrokerError(RuntimeError):
    pass


_SAFE_REL = re.compile(r"^[0-9a-fA-F-]{20,80}/[0-9a-fA-F]{40}$")
_SAFE_AGENT = {"aider", "openhands", "ci"}


def _secret() -> str:
    value = os.getenv("SANDBOX_BROKER_SECRET", "")
    if len(value) < 32:
        raise BrokerError("SANDBOX_BROKER_SECRET must be at least 32 characters")
    return value


def _run(args: list[str], *, timeout: int, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "docker command failed")[-2000:]
        raise BrokerError(detail)
    return result


def _security_args(*, network: str, volume_name: str, memory: str, cpus: str, pids: int) -> list[str]:
    return [
        "--rm",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", str(pids),
        "--memory", memory,
        "--cpus", cpus,
        "--user", "10001:10001",
        "--label", "amarktai.sandbox=true",
        "--network", network,
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
        "--tmpfs", "/home/sandbox:rw,nosuid,nodev,size=96m",
        "-v", f"{volume_name}:/workspace:rw",
    ]


def cleanup_stale_containers(max_age_seconds: int) -> dict[str, int]:
    max_age_seconds = max(60, min(int(max_age_seconds), 86400))
    listed = _run(["docker", "ps", "-aq", "--filter", "label=amarktai.sandbox=true"], timeout=30, check=False)
    removed = inspected = 0
    now = datetime.now(timezone.utc)
    for container_id in (listed.stdout or "").splitlines()[:1000]:
        container_id = container_id.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            continue
        info = _run(["docker", "inspect", "-f", "{{.State.Running}}|{{.Created}}", container_id], timeout=15, check=False)
        if info.returncode != 0 or "|" not in (info.stdout or ""):
            continue
        running, created_raw = info.stdout.strip().split("|", 1)
        inspected += 1
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if running != "true" or (now - created).total_seconds() >= max_age_seconds:
            removed += int(_run(["docker", "rm", "-f", container_id], timeout=30, check=False).returncode == 0)
    return {"inspected": inspected, "removed": removed}


def run_sandbox(payload: dict[str, Any]) -> dict[str, Any]:
    agent = str(payload.get("agent") or "")
    if agent not in _SAFE_AGENT:
        raise BrokerError("unsupported sandbox agent")
    snapshot_rel = str(payload.get("snapshot_rel") or "")
    if not _SAFE_REL.fullmatch(snapshot_rel):
        raise BrokerError("snapshot path is not an approved job/commit path")
    task = str(payload.get("task") or "").strip()
    if not task or len(task) > 50000:
        raise BrokerError("sandbox task is empty or too large")
    test_command = str(payload.get("test_command") or "").strip()
    if not test_command or len(test_command) > 4000:
        raise BrokerError("explicit test command is required")
    model = str(payload.get("model") or "").strip()
    scoped_token = str(payload.get("scoped_token") or "").strip()
    if agent in {"aider", "openhands"} and (not model or not scoped_token):
        raise BrokerError("AI coding sandbox requires scoped model credentials")

    image = os.getenv("SANDBOX_AGENT_IMAGE", "amarktai-earn-sandbox:phase8c")
    repo_volume = os.getenv("SANDBOX_REPOSITORY_VOLUME", "amarktai_repo_cache")
    llm_network = os.getenv("SANDBOX_LLM_NETWORK", "amarktai_sandbox_llm")
    proxy_url = os.getenv("SANDBOX_LLM_BASE_URL", "http://genx-proxy:8081/v1")
    memory = os.getenv("SANDBOX_MEMORY_LIMIT", "1024m")
    cpus = os.getenv("SANDBOX_CPU_LIMIT", "1.5")
    pids = max(32, min(int(os.getenv("SANDBOX_PIDS_LIMIT", "256")), 1024))
    timeout = max(30, min(int(os.getenv("SANDBOX_EXECUTION_TIMEOUT_SECONDS", "900")), 3600))
    volume_name = "amarktai_job_" + secrets.token_hex(12)
    sandbox_id = secrets.token_hex(12)
    agent_log = ""
    test_log = ""
    patch = ""
    agent_exit = 1
    test_exit = 1
    try:
        _run(["docker", "volume", "create", volume_name], timeout=30)
        seed = [
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--user", "0:0",
            "-v", f"{repo_volume}:/src:ro", "-v", f"{volume_name}:/workspace:rw",
            "-e", f"SNAPSHOT_REL={snapshot_rel}", image,
            "/bin/bash", "-lc",
            'cp -a "/src/$SNAPSHOT_REL/." /workspace/ && cd /workspace && git init -q && git config user.name "Amarktai Sandbox" && git config user.email "sandbox@amarktai.invalid" && git add -A && git commit -qm baseline && git tag amarktai-baseline && chown -R 10001:10001 /workspace',
        ]
        _run(seed, timeout=120)

        network = "none" if agent == "ci" else llm_network
        args = ["docker", "run", *_security_args(network=network, volume_name=volume_name, memory=memory, cpus=cpus, pids=pids)]
        args += ["-e", f"AMARKTAI_AGENT={agent}", "-e", f"AMARKTAI_TASK={task}", "-e", f"AMARKTAI_TEST_COMMAND={test_command}"]
        if agent in {"aider", "openhands"}:
            args += [
                "-e", f"LLM_API_KEY={scoped_token}",
                "-e", f"LLM_MODEL=openai/{model}",
                "-e", f"LLM_BASE_URL={proxy_url}",
                "-e", f"AIDER_OPENAI_API_KEY={scoped_token}",
                "-e", f"AIDER_OPENAI_API_BASE={proxy_url}",
                "-e", f"AIDER_MODEL=openai/{model}",
            ]
        args += [image, "/opt/amarktai/run-agent"]
        result = _run(args, timeout=timeout, check=False)
        agent_exit = result.returncode
        agent_log = ((result.stdout or "") + "\n" + (result.stderr or ""))[-200000:]

        diff_result = _run([
            "docker", "run", *_security_args(
                network="none", volume_name=volume_name, memory=memory, cpus=cpus, pids=pids,
            ), image, "/bin/bash", "-lc",
            "cd /workspace && git add -N -- . && git diff --binary --no-ext-diff amarktai-baseline --",
        ], timeout=120, check=False)
        patch = diff_result.stdout or ""

        if agent == "ci":
            test_exit = agent_exit
            test_log = agent_log
        else:
            test_result = _run([
                "docker", "run", *_security_args(network="none", volume_name=volume_name, memory=memory, cpus=cpus, pids=pids),
                "-e", f"AMARKTAI_TEST_COMMAND={test_command}", image,
                "/bin/bash", "-lc", 'cd /workspace && /bin/bash -lc "$AMARKTAI_TEST_COMMAND"',
            ], timeout=timeout, check=False)
            test_exit = test_result.returncode
            test_log = ((test_result.stdout or "") + "\n" + (test_result.stderr or ""))[-200000:]
        return {
            "sandbox_id": sandbox_id,
            "agent": agent,
            "agent_exit_code": agent_exit,
            "test_exit_code": test_exit,
            "patch": patch,
            "agent_log": agent_log,
            "test_log": test_log,
            "limits": {"memory": memory, "cpus": cpus, "pids": pids, "timeout_seconds": timeout},
        }
    finally:
        _run(["docker", "volume", "rm", "-f", volume_name], timeout=30, check=False)


class Handler(BaseHTTPRequestHandler):
    server_version = "AmarktaiSandboxBroker/1"

    def log_message(self, fmt, *args):
        return

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in {"/run", "/cleanup"}:
            return self._json(404, {"error": "not found"})
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {_secret()}"):
            return self._json(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2 * 1024 * 1024:
                raise BrokerError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise BrokerError("request body must be an object")
            if self.path == "/cleanup":
                return self._json(200, cleanup_stale_containers(int(payload.get("max_age_seconds", 1800))))
            return self._json(200, run_sandbox(payload))
        except BrokerError as exc:
            return self._json(400, {"error": str(exc)})
        except subprocess.TimeoutExpired:
            return self._json(408, {"error": "sandbox timed out"})
        except Exception as exc:
            return self._json(500, {"error": exc.__class__.__name__})


def main():
    host = os.getenv("SANDBOX_BROKER_HOST", "0.0.0.0")
    port = int(os.getenv("SANDBOX_BROKER_PORT", "8090"))
    _secret()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
