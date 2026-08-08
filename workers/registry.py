from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import shutil


class WorkerRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerSpec:
    worker_class: str
    version: str
    factory: str
    operations: tuple[str, ...]
    qa_profile: str
    description: str
    input_suffixes: tuple[str, ...] = ()
    requires_genx: bool = False
    runtime_commands: tuple[str, ...] = ()

    def build(self):
        module_name, attr = self.factory.rsplit(".", 1)
        factory = getattr(import_module(module_name), attr)
        return factory()


_SPECS: tuple[WorkerSpec, ...] = (
    WorkerSpec(
        worker_class="structured_data",
        version="1.1.0",
        factory="workers.structured_data.worker.StructuredDataWorker",
        operations=("json_to_csv", "csv_normalize"),
        qa_profile="csv",
        description="Deterministic JSON/CSV conversion and normalization",
        input_suffixes=(".json", ".csv"),
    ),
    WorkerSpec(
        worker_class="documents",
        version="1.0.0",
        factory="workers.documents.worker.DocumentsWorker",
        operations=("document_extract_text", "document_summarize", "document_rewrite"),
        qa_profile="document",
        description="Document extraction, summarisation and rewrite for PDF/DOCX/TXT/Markdown",
        input_suffixes=(".pdf", ".docx", ".txt", ".md"),
        requires_genx=True,
    ),
    WorkerSpec(
        worker_class="research",
        version="1.0.0",
        factory="workers.research.worker.ResearchWorker",
        operations=("research_report",),
        qa_profile="research",
        description="Web-assisted research reports with explicit source citations",
        input_suffixes=(),
        requires_genx=True,
    ),
    WorkerSpec(
        worker_class="localization",
        version="1.0.0",
        factory="workers.localization.worker.LocalizationWorker",
        operations=("translate_document",),
        qa_profile="translation",
        description="Document/text localisation into an explicitly requested target language",
        input_suffixes=(".pdf", ".docx", ".txt", ".md"),
        requires_genx=True,
    ),
    WorkerSpec(
        worker_class="transcription",
        version="1.0.0",
        factory="workers.transcription.worker.TranscriptionWorker",
        operations=("transcribe_media",),
        qa_profile="transcript",
        description="Audio/video transcription using a live GenX transcription-capable model",
        input_suffixes=(".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4", ".mov", ".webm"),
        requires_genx=True,
    ),
    WorkerSpec(
        worker_class="media",
        version="1.0.0",
        factory="workers.media.worker.MediaWorker",
        operations=(
            "image_resize", "image_center_crop", "image_convert", "image_compress", "image_thumbnail",
            "media_trim", "media_transcode", "media_extract_audio",
        ),
        qa_profile="media",
        description="Bounded deterministic image, audio, and video transformations",
        input_suffixes=(".jpg", ".jpeg", ".png", ".webp", ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4", ".mov", ".webm"),
        requires_genx=False,
        runtime_commands=("ffmpeg", "ffprobe"),
    ),
    WorkerSpec(
        worker_class="advanced_structured_data",
        version="1.0.0",
        factory="workers.advanced_structured_data.worker.AdvancedStructuredDataWorker",
        operations=(
            "tabular_convert", "tabular_normalize", "tabular_deduplicate", "tabular_merge_join",
            "tabular_filter_sort", "tabular_column_map", "tabular_schema_validate",
        ),
        qa_profile="tabular",
        description="Bounded CSV/JSON/XLSX conversion, cleanup, schema, merge, mapping, filtering, sorting, and deduplication",
        input_suffixes=(".csv", ".json", ".xlsx"),
    ),
    WorkerSpec(
        worker_class="spreadsheet_reporting",
        version="1.0.0",
        factory="workers.spreadsheet_reporting.worker.SpreadsheetReportingWorker",
        operations=("spreadsheet_report",),
        qa_profile="spreadsheet",
        description="Professional multi-sheet XLSX reports with tables, summaries, aggregations, and bounded charts",
        input_suffixes=(".csv", ".json", ".xlsx"),
    ),
    WorkerSpec(
        worker_class="data_analysis",
        version="1.0.0",
        factory="workers.data_analysis.worker.DataAnalysisWorker",
        operations=("data_analysis_report",),
        qa_profile="analysis",
        description="Descriptive analysis, data-quality reporting, grouping, trends, and basic visualization artifacts",
        input_suffixes=(".csv", ".json", ".xlsx"),
    ),
    WorkerSpec(
        worker_class="technical_documentation",
        version="1.0.0",
        factory="workers.professional_text.worker.TechnicalDocumentationWorker",
        operations=("technical_documentation",),
        qa_profile="professional_text",
        description="README, API, installation, runbook, release-note, and codebase documentation grounded in supplied evidence",
        input_suffixes=(".pdf", ".docx", ".txt", ".md"),
        requires_genx=True,
    ),
    WorkerSpec(
        worker_class="content_copy",
        version="1.0.0",
        factory="workers.professional_text.worker.ContentCopyWorker",
        operations=("content_package",),
        qa_profile="professional_text",
        description="Articles, landing-page copy, product descriptions, marketing copy, FAQ, and social packages",
        input_suffixes=(".pdf", ".docx", ".txt", ".md"),
        requires_genx=True,
    ),
    WorkerSpec(
        worker_class="seo_audit",
        version="1.0.0",
        factory="workers.seo_audit.worker.SEOAuditWorker",
        operations=("seo_content_audit",),
        qa_profile="seo_audit",
        description="Supplied or policy-approved page structure, accessibility, metadata, keyword, and content-gap audit",
        input_suffixes=(".html", ".htm"),
    ),
    WorkerSpec(
        worker_class="presentations",
        version="1.0.0",
        factory="workers.presentations.worker.PresentationWorker",
        operations=("presentation_create",),
        qa_profile="presentation",
        description="Deterministic PPTX presentation production with bounded slides and structural reopen verification",
        input_suffixes=(".txt", ".md"),
    ),
    WorkerSpec(
        worker_class="document_production",
        version="1.0.0",
        factory="workers.document_production.worker.DocumentProductionWorker",
        operations=("docx_create", "pdf_create"),
        qa_profile="produced_document",
        description="Polished deterministic DOCX and PDF deliverable production",
        input_suffixes=(".txt", ".md"),
    ),
    WorkerSpec(
        worker_class="public_web_data",
        version="1.0.0",
        factory="workers.public_web_data.worker.PublicWebDataWorker",
        operations=("public_web_extract",),
        qa_profile="public_web",
        description="Explicitly authorized, robots-aware, HTTPS-only, bounded public-page retrieval and extraction",
    ),
    WorkerSpec(
        worker_class="web_output",
        version="1.0.0",
        factory="workers.web_output.worker.StaticWebOutputWorker",
        operations=("static_html_create",),
        qa_profile="static_html",
        description="Accessible static HTML/CSS artifact generation without third-party deployment",
        input_suffixes=(".txt", ".md"),
    ),
    WorkerSpec(
        worker_class="defensive_code_review",
        version="1.0.0",
        factory="workers.defensive_code_review.worker.DefensiveCodeReviewWorker",
        operations=("defensive_code_review",),
        qa_profile="defensive_review",
        description="Authorized supplied-repository quality, test-gap, dependency/config, and secure-code review",
    ),
    WorkerSpec(
        worker_class="customer_support",
        version="1.0.0",
        factory="workers.professional_text.worker.CustomerSupportWorker",
        operations=("support_content_package",),
        qa_profile="professional_text",
        description="Draft support replies, knowledge-base content, ticket summaries, and FAQ packages without autonomous sending",
        input_suffixes=(".pdf", ".docx", ".txt", ".md"),
        requires_genx=True,
    ),
    WorkerSpec(
        worker_class="code_small",
        version="1.0.0",
        factory="workers.coding.aider_worker.AiderCodingWorker",
        operations=("code_change_small",),
        qa_profile="code_patch",
        description="Small repository changes executed by Aider inside a disposable constrained sandbox",
        input_suffixes=(),
        requires_genx=True,
    ),
    WorkerSpec(
        worker_class="code_heavy",
        version="1.0.0",
        factory="workers.coding.openhands_worker.OpenHandsCodingWorker",
        operations=("code_change_heavy",),
        qa_profile="code_patch",
        description="Complex repository changes executed by OpenHands inside a disposable constrained sandbox",
        input_suffixes=(),
        requires_genx=True,
    ),
    WorkerSpec(
        worker_class="ci_testing",
        version="1.0.0",
        factory="workers.ci_testing.worker.CITestingWorker",
        operations=("run_repository_tests",),
        qa_profile="ci",
        description="Independent repository test execution inside a disposable network-isolated sandbox",
        input_suffixes=(),
        requires_genx=False,
    ),
    WorkerSpec(
        worker_class="synthetic_data",
        version="1.0.0",
        factory="workers.synthetic_data.worker.SyntheticDataWorker",
        operations=("synthetic_dataset_generate",),
        qa_profile="synthetic_dataset",
        description="Commissioned schema-driven synthetic datasets with validation, privacy/provenance gates, splits, cards, and independent reopen QA",
        input_suffixes=(),
        requires_genx=False,
    ),
    WorkerSpec(
        worker_class="ai_safety_research",
        version="1.0.0",
        factory="workers.ai_safety_research.worker.AISafetyResearchWorker",
        operations=("ai_safety_evaluate",),
        qa_profile="ai_safety_research",
        description="Persisted-scope, bounded, offline AI-safety evaluation of supplied/local authorized targets only",
        input_suffixes=(),
        requires_genx=False,
    ),
)

_BY_CLASS = {spec.worker_class: spec for spec in _SPECS}
_BY_OPERATION = {operation: spec for spec in _SPECS for operation in spec.operations}

if len(_BY_CLASS) != len(_SPECS):
    raise RuntimeError("duplicate worker_class in worker registry")
if sum(len(spec.operations) for spec in _SPECS) != len(_BY_OPERATION):
    raise RuntimeError("duplicate operation in worker registry")


def all_specs() -> tuple[WorkerSpec, ...]:
    return _SPECS


def worker_spec(worker_class: str) -> WorkerSpec:
    try:
        return _BY_CLASS[worker_class]
    except KeyError as exc:
        raise WorkerRegistryError(f"unknown worker class: {worker_class}") from exc


def operation_spec(operation: str) -> WorkerSpec:
    try:
        return _BY_OPERATION[operation]
    except KeyError as exc:
        raise WorkerRegistryError(f"unsupported worker operation: {operation}") from exc


def supports_operation(worker_class: str, operation: str) -> bool:
    spec = _BY_CLASS.get(worker_class)
    return bool(spec and operation in spec.operations)


def registered_operations() -> tuple[str, ...]:
    return tuple(sorted(_BY_OPERATION))


def registry_manifest() -> list[dict[str, object]]:
    return [
        {
            "worker_class": spec.worker_class,
            "version": spec.version,
            "operations": list(spec.operations),
            "qa_profile": spec.qa_profile,
            "description": spec.description,
            "input_suffixes": list(spec.input_suffixes),
            "requires_genx": spec.requires_genx,
            "runtime_commands": list(spec.runtime_commands),
            "runtime_available": all(shutil.which(command) for command in spec.runtime_commands),
        }
        for spec in _SPECS
    ]
