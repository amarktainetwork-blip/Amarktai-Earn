from __future__ import annotations

from pathlib import Path

from workers.base import WorkRequest, WorkResult, Worker
from workers.genx_support import GenXWorkerError, generate_text
from workers.text_extract import TextExtractionError, extract_text


class DocumentsWorker(Worker):
    worker_class = "documents"

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            source = Path(request.inputs["source"])
            operation = str(request.inputs.get("operation") or "")
            text = extract_text(source)
            request.workspace.mkdir(parents=True, exist_ok=True)
            if operation == "document_extract_text":
                output = text
                model = None
            elif operation == "document_summarize":
                prompt = (
                    "Summarize the source document faithfully. Preserve important facts, numbers, constraints and named entities. "
                    "Do not add facts not present in the source. Return polished Markdown.\n\nSOURCE:\n" + text
                )
                output, call = generate_text(request, prompt=prompt, task_class="document_summarize")
                model = call.model
            elif operation == "document_rewrite":
                instructions = str(request.inputs.get("instructions") or "Rewrite clearly and professionally while preserving meaning.")
                prompt = f"Rewrite this document according to the instructions. Do not invent facts.\nINSTRUCTIONS: {instructions}\n\nSOURCE:\n{text}"
                output, call = generate_text(request, prompt=prompt, task_class="document_rewrite")
                model = call.model
            else:
                return WorkResult(ok=False, error=f"unsupported operation: {operation}")
            target = request.workspace / ("extracted.txt" if operation == "document_extract_text" else "document.md")
            target.write_text(output.strip() + "\n", encoding="utf-8")
            return WorkResult(
                ok=True,
                artifacts=[target],
                evidence={"source_chars": len(text), "output_chars": len(output), "operation": operation, "model": model or "deterministic"},
            )
        except (OSError, KeyError, TextExtractionError, GenXWorkerError, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))
