from __future__ import annotations

from workers.base import WorkRequest, WorkResult, Worker
from workers.genx_support import GenXWorkerError, research_with_web


_RESEARCH_OPERATIONS = {
    "research_report": "Produce a general evidence-based report answering the research query.",
    "competitor_research": "Compare the named competitors or alternatives using current evidence, separating verified facts from inference and highlighting material differences.",
    "market_research": "Assess the requested market using current evidence: demand signals, customer segments, competitors, pricing or economics when available, risks, and uncertainties.",
    "website_research": "Research the specified website, organization, product, or service using current public evidence. Distinguish claims made by the subject from independent evidence.",
    "multi_source_research": "Synthesize the query across multiple independent sources, identify agreement and disagreement, and state the strongest supported conclusion.",
    "fact_extraction_research": "Extract the requested facts from current sources. Use a concise structured report, cite each material fact, and explicitly mark anything not verified.",
}


class ResearchWorker(Worker):
    worker_class = "research"

    @staticmethod
    def _materialize_report(
        request: WorkRequest,
        report: str,
        sources: list[str],
        *,
        model: str,
        recovered: bool,
        operation: str = "research_report",
    ) -> WorkResult:
        report = str(report or "").strip()
        if not report:
            return WorkResult(ok=False, error="research report text is required")
        request.workspace.mkdir(parents=True, exist_ok=True)
        filename = "research-report.md" if operation == "research_report" else f"{operation.replace('_', '-')}.md"
        target = request.workspace / filename
        target.write_text(report + "\n", encoding="utf-8")
        clean_sources = list(dict.fromkeys(str(value) for value in sources if str(value).startswith("https://")))
        return WorkResult(
            ok=True,
            artifacts=[target],
            evidence={
                "operation": operation,
                "sources": clean_sources,
                "source_count": len(clean_sources),
                "output_chars": len(report),
                "model": model,
                "recovered_completed_provider_result": recovered,
            },
        )

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            operation = str(request.inputs.get("operation") or "")
            instruction = _RESEARCH_OPERATIONS.get(operation)
            if instruction is None:
                return WorkResult(ok=False, error="unsupported research operation")
            query = str(request.inputs.get("query") or "").strip()
            if not query:
                return WorkResult(ok=False, error="research query is required")
            requested = str(request.inputs.get("requirements") or "").strip()
            requirements = f"{instruction}\n{requested}".strip()
            report, sources, call = research_with_web(
                request,
                query=query,
                requirements=requirements,
            )
            return self._materialize_report(
                request,
                report,
                sources,
                model=call.model,
                recovered=False,
                operation=operation,
            )
        except (OSError, GenXWorkerError, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))

    def recover_completed_provider_result(self, request: WorkRequest) -> WorkResult:
        """Materialize authoritative completed Research output without a provider POST."""
        try:
            operation = str(request.inputs.get("operation") or "")
            if operation not in _RESEARCH_OPERATIONS:
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
                operation=operation,
            )
        except OSError as exc:
            return WorkResult(ok=False, error=str(exc))
