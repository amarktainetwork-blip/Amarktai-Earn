from __future__ import annotations

from workers.base import WorkRequest, WorkResult, Worker
from workers.genx_support import GenXWorkerError, research_with_web


class ResearchWorker(Worker):
    worker_class = "research"

    @staticmethod
    def _materialize_report(request: WorkRequest, report: str, sources: list[str], *, model: str, recovered: bool) -> WorkResult:
        report = str(report or "").strip()
        if not report:
            return WorkResult(ok=False, error="research report text is required")
        request.workspace.mkdir(parents=True, exist_ok=True)
        target = request.workspace / "research-report.md"
        target.write_text(report + "\n", encoding="utf-8")
        return WorkResult(
            ok=True,
            artifacts=[target],
            evidence={
                "sources": list(dict.fromkeys(str(value) for value in sources if str(value).startswith("https://"))),
                "source_count": len(list(dict.fromkeys(str(value) for value in sources if str(value).startswith("https://")))),
                "output_chars": len(report),
                "model": model,
                "recovered_completed_provider_result": recovered,
            },
        )

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
            return self._materialize_report(
                request,
                report,
                sources,
                model=call.model,
                recovered=False,
            )
        except (OSError, GenXWorkerError, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))

    def recover_completed_provider_result(self, request: WorkRequest) -> WorkResult:
        """Materialize authoritative completed Research output without a provider POST."""
        try:
            if request.inputs.get("operation") != "research_report":
                return WorkResult(ok=False, error="unsupported research recovery operation")
            report = str(request.inputs.get("recovered_provider_text") or "").strip()
            sources = request.inputs.get("recovered_sources") or []
            if not isinstance(sources, list):
                return WorkResult(ok=False, error="recovered research sources must be a list")
            return self._materialize_report(
                request,
                report,
                sources,
                model=str(request.inputs.get("recovered_model") or ""),
                recovered=True,
            )
        except OSError as exc:
            return WorkResult(ok=False, error=str(exc))
