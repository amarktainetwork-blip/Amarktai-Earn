from __future__ import annotations

import json

from workers.base import WorkRequest, WorkResult, Worker
from workers.genx_support import GenXWorkerError, generate_text


_OPERATIONS = {
    "structured_json_generate": "Return JSON matching the requested structure or schema.",
    "classify_text": "Classify the supplied text. Return a JSON object with at least a label field and, when useful, confidence and rationale fields.",
    "extract_structured_facts": "Extract only facts supported by the supplied material. Return structured JSON and do not invent missing values.",
}


def _decode_json_output(raw: str):
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


class StructuredSemanticWorker(Worker):
    worker_class = "structured_semantic"

    def execute(self, request: WorkRequest) -> WorkResult:
        operation = str(request.inputs.get("operation") or "")
        instruction = _OPERATIONS.get(operation)
        if instruction is None:
            return WorkResult(ok=False, error="unsupported structured semantic operation")
        source = str(
            request.inputs.get("text")
            or request.inputs.get("prompt")
            or request.inputs.get("source_text")
            or ""
        ).strip()
        if not source:
            return WorkResult(ok=False, error="structured semantic source text is required")
        schema = request.inputs.get("schema")
        labels = request.inputs.get("labels")
        requirements = str(request.inputs.get("requirements") or "").strip()
        prompt = (
            f"Task: {operation}\n"
            f"Instruction: {instruction}\n"
            "Output must be valid JSON only: no Markdown fences and no commentary outside JSON.\n"
            f"Requested schema: {json.dumps(schema, ensure_ascii=False) if schema is not None else '(not supplied)'}\n"
            f"Allowed labels: {json.dumps(labels, ensure_ascii=False) if labels is not None else '(not supplied)'}\n"
            f"Requirements: {requirements or '(none)'}\n\n"
            f"Source material:\n{source}"
        )
        try:
            output, call = generate_text(request, prompt=prompt, task_class=operation)
            payload = _decode_json_output(output)
            if not isinstance(payload, (dict, list)):
                return WorkResult(ok=False, error="structured semantic output must be a JSON object or array")
            if operation == "classify_text" and not isinstance(payload, dict):
                return WorkResult(ok=False, error="classification output must be a JSON object")
            if operation == "classify_text" and not str(payload.get("label") or "").strip():
                return WorkResult(ok=False, error="classification output is missing label")
            request.workspace.mkdir(parents=True, exist_ok=True)
            target = request.workspace / f"{operation.replace('_', '-')}.json"
            target.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            return WorkResult(
                ok=True,
                artifacts=[target],
                evidence={
                    "operation": operation,
                    "json_type": type(payload).__name__,
                    "classification_label_present": bool(isinstance(payload, dict) and payload.get("label")),
                    "source_chars": len(source),
                    "model": call.model,
                },
            )
        except (OSError, ValueError, json.JSONDecodeError, GenXWorkerError) as exc:
            return WorkResult(ok=False, error=str(exc))
