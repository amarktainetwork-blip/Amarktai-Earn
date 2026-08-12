from __future__ import annotations

from workers.base import WorkRequest, WorkResult, Worker
from workers.coding.common import CodingWorkerError, run_ai_coding_sandbox


class OpenHandsCodingWorker(Worker):
    worker_class = "code_heavy"

    def execute(self, request: WorkRequest) -> WorkResult:
        if request.inputs.get("operation") != "code_change_heavy":
            return WorkResult(ok=False, error="unsupported heavy-coding operation")
        try:
            result = run_ai_coding_sandbox(request, agent="openhands")
            patch = request.workspace / "changes.patch.txt"
            patch.write_text(str(result.get("patch") or ""), encoding="utf-8")
            log = request.workspace / "sandbox.log"
            log.write_text(str(result.get("agent_log") or ""), encoding="utf-8")
            test_log = request.workspace / "test.log"
            test_log.write_text(str(result.get("test_log") or ""), encoding="utf-8")
            return WorkResult(
                ok=int(result.get("agent_exit_code", 1)) == 0,
                artifacts=[patch, log, test_log],
                evidence={
                    "sandbox_agent": "openhands",
                    "sandbox_id": result.get("sandbox_id"),
                    "agent_exit_code": int(result.get("agent_exit_code", 1)),
                    "test_exit_code": int(result.get("test_exit_code", 1)),
                    "patch_bytes": len(patch.read_bytes()),
                    "limits": result.get("limits") if isinstance(result.get("limits"), dict) else {},
                },
                error=None if int(result.get("agent_exit_code", 1)) == 0 else "OpenHands sandbox exited non-zero",
            )
        except CodingWorkerError as exc:
            return WorkResult(ok=False, error=str(exc))
