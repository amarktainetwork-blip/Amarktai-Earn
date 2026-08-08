from __future__ import annotations

from workers.base import WorkRequest, WorkResult, Worker
from workers.genx_support import GenXWorkerError, research_with_web


class ResearchWorker(Worker):
    worker_class = "research"

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            if request.inputs.get("operation") != "research_report":
                return WorkResult(ok=False, error="unsupported research operation")
            query = str(request.inputs.get("query") or "").strip()
            if not query:
                return WorkResult(ok=False, error="research query is required")
            report, sources, call = research_with_web(
                request,
                query=query,
                requirements=str(request.inputs.get("requirements") or ""),
            )
            request.workspace.mkdir(parents=True, exist_ok=True)
            target = request.workspace / "research-report.md"
            target.write_text(report.strip() + "\n", encoding="utf-8")
            return WorkResult(
                ok=True,
                artifacts=[target],
                evidence={"sources": sources, "source_count": len(sources), "output_chars": len(report), "model": call.model},
            )
        except (OSError, GenXWorkerError, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))
