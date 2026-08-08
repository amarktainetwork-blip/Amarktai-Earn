from __future__ import annotations

import os
from pathlib import Path

from workers.base import WorkRequest, WorkResult, Worker
from workers.genx_support import GenXWorkerError, generate_text
from workers.text_extract import TextExtractionError, extract_text


def _source_context(inputs: dict) -> str:
    source = inputs.get("source")
    if not source:
        return ""
    return extract_text(Path(str(source)), max_chars=max(1000, int(os.getenv("PROFESSIONAL_TEXT_MAX_SOURCE_CHARS", "200000"))))


def _repository_context(path: Path) -> str:
    max_files = max(1, int(os.getenv("DOCUMENTATION_MAX_REPOSITORY_FILES", "500")))
    max_chars = max(1000, int(os.getenv("DOCUMENTATION_MAX_REPOSITORY_CONTEXT_CHARS", "120000")))
    ignored = {".git", ".venv", "node_modules", "dist", "build", "vendor", "__pycache__"}
    allowed = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs", ".rb", ".php", ".md", ".toml", ".yaml", ".yml", ".json"}
    parts = []
    for candidate in sorted(path.rglob("*")):
        if len(parts) >= max_files or sum(len(item) for item in parts) >= max_chars:
            break
        if not candidate.is_file() or candidate.is_symlink() or any(part in ignored for part in candidate.parts) or candidate.suffix.casefold() not in allowed:
            continue
        try:
            body = candidate.read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            continue
        parts.append(f"\n--- {candidate.relative_to(path).as_posix()} ---\n{body}")
    return "".join(parts)[:max_chars]


class _ProfessionalTextWorker(Worker):
    operation = ""
    output_name = "deliverable.md"
    allowed_types: tuple[str, ...] = ()
    type_field = "deliverable_type"
    task_class = "professional_text"
    base_instruction = ""

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            if request.inputs.get("operation") != self.operation:
                return WorkResult(ok=False, error=f"unsupported operation: {request.inputs.get('operation')}")
            deliverable_type = str(request.inputs.get(self.type_field) or self.allowed_types[0]).casefold().replace("-", "_")
            if deliverable_type not in self.allowed_types:
                return WorkResult(ok=False, error=f"unsupported {self.type_field}")
            requirements = str(request.inputs.get("requirements") or request.inputs.get("brief") or "").strip()
            context = _source_context(request.inputs)
            prompt = (
                f"{self.base_instruction}\nDeliverable type: {deliverable_type}.\n"
                f"Requirements:\n{requirements or 'Produce a concise, professional, internally consistent deliverable.'}\n\n"
                f"Supplied source material (do not invent facts beyond it):\n{context or '(none supplied)'}"
            )
            output, call = generate_text(request, prompt=prompt, task_class=self.task_class)
            request.workspace.mkdir(parents=True, exist_ok=True)
            target = request.workspace / self.output_name
            target.write_text(output.strip() + "\n", encoding="utf-8")
            return WorkResult(ok=True, artifacts=[target], evidence={
                "operation": self.operation, "deliverable_type": deliverable_type,
                "output_chars": len(output.strip()), "source_chars": len(context),
                "model": call.model, "draft_only": self.operation == "support_content_package",
            })
        except (OSError, KeyError, ValueError, GenXWorkerError, TextExtractionError) as exc:
            return WorkResult(ok=False, error=str(exc))


class TechnicalDocumentationWorker(_ProfessionalTextWorker):
    worker_class = "technical_documentation"
    operation = "technical_documentation"
    output_name = "technical-documentation.md"
    allowed_types = ("readme", "api_documentation", "installation_guide", "runbook", "release_notes", "codebase_documentation")
    type_field = "documentation_type"
    task_class = "technical_documentation"
    base_instruction = (
        "Produce technically precise Markdown documentation. Use only supplied repository/source evidence, make commands explicit, "
        "call out prerequisites and uncertainty, and never claim an unverified deployment or runtime result."
    )

    def execute(self, request: WorkRequest) -> WorkResult:
        repository = request.inputs.get("repository_path")
        if repository:
            try:
                existing = str(request.inputs.get("requirements") or request.inputs.get("brief") or "")
                request.inputs = {**request.inputs, "requirements": existing + "\n\nREPOSITORY EVIDENCE:\n" + _repository_context(Path(str(repository)))}
            except (OSError, ValueError) as exc:
                return WorkResult(ok=False, error=str(exc))
        return super().execute(request)


class ContentCopyWorker(_ProfessionalTextWorker):
    worker_class = "content_copy"
    operation = "content_package"
    output_name = "content-package.md"
    allowed_types = ("article", "landing_page", "product_descriptions", "marketing_copy", "faq", "social_copy")
    type_field = "content_type"
    task_class = "content_copy"
    base_instruction = (
        "Create original, useful professional copy in the requested structure and tone. Do not fabricate product claims, citations, "
        "testimonials, guarantees, or regulated advice. Clearly separate variants where multiple items are requested."
    )


class CustomerSupportWorker(_ProfessionalTextWorker):
    worker_class = "customer_support"
    operation = "support_content_package"
    output_name = "support-content-package.md"
    allowed_types = ("reply_draft", "knowledge_base", "ticket_summary", "faq")
    type_field = "support_content_type"
    task_class = "customer_support_content"
    base_instruction = (
        "Create a draft support deliverable grounded in the supplied case facts. Preserve identifiers and commitments exactly, avoid "
        "inventing policy or account actions, identify missing information, and do not claim the message was sent."
    )
