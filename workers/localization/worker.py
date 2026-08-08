from __future__ import annotations

from pathlib import Path

from workers.base import WorkRequest, WorkResult, Worker
from workers.genx_support import GenXWorkerError, generate_text
from workers.text_extract import TextExtractionError, extract_text


class LocalizationWorker(Worker):
    worker_class = "localization"

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            if request.inputs.get("operation") != "translate_document":
                return WorkResult(ok=False, error="unsupported localization operation")
            source = Path(request.inputs["source"])
            target_language = str(request.inputs.get("target_language") or "").strip()
            if not target_language:
                return WorkResult(ok=False, error="target language is required")
            text = extract_text(source)
            prompt = (
                f"Translate the following document into {target_language}. Preserve meaning, numbers, names, structure and formatting. "
                "Do not summarize or add information. Return only the translated document.\n\nSOURCE:\n" + text
            )
            output, call = generate_text(
                request,
                prompt=prompt,
                task_class="translation",
                capability_keywords=("translation", "translate", "localization"),
            )
            request.workspace.mkdir(parents=True, exist_ok=True)
            target = request.workspace / "translation.md"
            target.write_text(output.strip() + "\n", encoding="utf-8")
            return WorkResult(
                ok=True,
                artifacts=[target],
                evidence={
                    "source_chars": len(text),
                    "output_chars": len(output),
                    "target_language": target_language,
                    "model": call.model,
                },
            )
        except (OSError, KeyError, TextExtractionError, GenXWorkerError, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))
