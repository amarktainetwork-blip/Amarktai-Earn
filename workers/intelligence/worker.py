from __future__ import annotations

from workers.base import WorkRequest, WorkResult, Worker
from workers.genx_support import GenXWorkerError, generate_text


_OPERATION_INSTRUCTIONS = {
    "intelligence_chat": "Respond conversationally and directly to the user's request. Preserve stated constraints and distinguish facts from uncertainty.",
    "intelligence_reason": "Solve the requested reasoning task carefully. Give a concise answer with the necessary supporting rationale, but do not expose private chain-of-thought.",
    "intelligence_qa": "Answer the supplied question using the supplied context when present. Do not invent missing facts; state uncertainty explicitly.",
    "intelligence_summarize": "Produce a faithful, concise summary of the supplied material. Preserve important qualifications, numbers, dates, and decisions.",
    "intelligence_rewrite": "Rewrite the supplied material to satisfy the requested tone, audience, format, or constraints without introducing unsupported claims.",
    "intelligence_analyze": "Analyze the supplied problem or material, identify the important findings and tradeoffs, and clearly separate evidence from inference.",
}


class IntelligenceWorker(Worker):
    worker_class = "intelligence"

    def execute(self, request: WorkRequest) -> WorkResult:
        operation = str(request.inputs.get("operation") or "")
        instruction = _OPERATION_INSTRUCTIONS.get(operation)
        if instruction is None:
            return WorkResult(ok=False, error="unsupported intelligence operation")

        prompt_text = str(
            request.inputs.get("prompt")
            or request.inputs.get("message")
            or request.inputs.get("question")
            or request.inputs.get("text")
            or ""
        ).strip()
        if not prompt_text:
            return WorkResult(ok=False, error="intelligence input text is required")
        context = str(request.inputs.get("context") or "").strip()
        requirements = str(request.inputs.get("requirements") or "").strip()
        prompt = (
            f"Task: {operation}\n"
            f"Instruction: {instruction}\n"
            f"Requirements: {requirements or '(none)'}\n\n"
            f"Context:\n{context or '(none supplied)'}\n\n"
            f"Input:\n{prompt_text}"
        )
        try:
            output, call = generate_text(
                request,
                prompt=prompt,
                task_class=operation,
            )
            request.workspace.mkdir(parents=True, exist_ok=True)
            target = request.workspace / f"{operation.replace('_', '-')}.md"
            target.write_text(output.strip() + "\n", encoding="utf-8")
            return WorkResult(
                ok=True,
                artifacts=[target],
                evidence={
                    "operation": operation,
                    "output_chars": len(output.strip()),
                    "input_chars": len(prompt_text),
                    "context_chars": len(context),
                    "model": call.model,
                },
            )
        except (OSError, ValueError, GenXWorkerError) as exc:
            return WorkResult(ok=False, error=str(exc))
