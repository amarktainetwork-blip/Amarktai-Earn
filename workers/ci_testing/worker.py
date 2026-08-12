from __future__ import annotations

from workers.base import WorkRequest, WorkResult, Worker
from workers.coding.common import CodingWorkerError, run_ci_sandbox


class CITestingWorker(Worker):
    worker_class = "ci_testing"

    def execute(self, request: WorkRequest) -> WorkResult:
        if request.inputs.get("operation") != "run_repository_tests":
            return WorkResult(ok=False, error="unsupported CI-testing operation")
        try:
            result = run_ci_sandbox(request)
            report = request.workspace / "test-report.txt"
            report.write_text(str(result.get("test_log") or result.get("agent_log") or ""), encoding="utf-8")
            exit_code = int(result.get("test_exit_code", result.get("agent_exit_code", 1)))
            return WorkResult(
                ok=True,
                artifacts=[report],
                evidence={
                    "sandbox_agent": "ci",
                    "sandbox_id": result.get("sandbox_id"),
                    "test_exit_code": exit_code,
                    "report_bytes": len(report.read_bytes()),
                    "limits": result.get("limits") if isinstance(result.get("limits"), dict) else {},
                },
            )
        except CodingWorkerError as exc:
            return WorkResult(ok=False, error=str(exc))
