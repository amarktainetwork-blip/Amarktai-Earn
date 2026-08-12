from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import inspect
from pathlib import Path
import shutil


class WorkerRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class OperationContract:
    """Launch contract derived from the canonical worker specification.

    Operation names are declared only once (in ``WorkerSpec.operations``).  This
    object is the normalized, machine-readable contract consumed by admission,
    proof tooling, CI, and the owner dashboard.
    """

    operation: str
    worker_class: str
    input_contract: str
    required_asset_types: tuple[str, ...]
    runtime_capability: str
    provider_category: str
    tool_requirements: tuple[str, ...]
    model_parameter_requirements: tuple[str, ...]
    webdock_compatible: bool
    external_side_effect: str
    cost_policy: str
    output_contract: str
    artifact_requirements: tuple[str, ...]
    qa_profile: str
    semantic_qa: bool
    repair_policy: str
    submission_policy: str
    failure_policy: str
    proof_class: str
    owner_action_blocker: str = ""


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
    runtime_capability: str = "deterministic_local"
    provider_category: str = "local"
    tool_requirements: tuple[str, ...] = ()
    model_parameter_requirements: tuple[str, ...] = ()
    external_side_effect: str = "workspace_write_only"
    semantic_qa: bool = False
    local_operations: tuple[str, ...] = ()

    def build(self):
        module_name, attr = self.factory.rsplit(".", 1)
        factory = getattr(import_module(module_name), attr)
        return factory()

    def contract(self, operation: str) -> OperationContract:
        if operation not in self.operations:
            raise WorkerRegistryError(f"operation {operation!r} is not owned by {self.worker_class!r}")
        uses_genx = self.requires_genx and operation not in self.local_operations
        blocker = "GENX_CREDENTIAL_AND_LIVE_CATALOG_REQUIRED" if uses_genx else ""
        return OperationContract(
            operation=operation,
            worker_class=self.worker_class,
            input_contract=f"{self.factory}.execute validates WorkRequest.inputs for {operation}",
            required_asset_types=self.input_suffixes,
            runtime_capability=self.runtime_capability,
            provider_category=self.provider_category if uses_genx else "local",
            tool_requirements=self.tool_requirements,
            model_parameter_requirements=self.model_parameter_requirements,
            webdock_compatible=True,
            external_side_effect=(
                f"paid_provider_call_and_{self.external_side_effect}"
                if uses_genx and self.external_side_effect != "workspace_write_only"
                else "paid_provider_call" if uses_genx else self.external_side_effect
            ),
            cost_policy=(
                "persisted_job_credit_envelope; per-call ceiling; actual usage and monetary value reconciled"
                if uses_genx
                else "no_external_paid_cost; bounded local runtime admission"
            ),
            output_contract="WorkResult with truthful terminal status, persisted artifact paths, and evidence",
            artifact_requirements=("at_least_one_nonempty_workspace_artifact", "independent_reopen_qa"),
            qa_profile=self.qa_profile,
            semantic_qa=self.semantic_qa and operation not in self.local_operations,
            repair_policy="bounded_new_execution_attempt_after_qa_failure_only",
            submission_policy="deterministic_qa_pass_and_acceptance_contract_required",
            failure_policy=(
                "confirmed zero-cost validation rejection is terminal compatibility evidence; any retry needs a new key and bounded policy; ambiguous remote state stops for reconciliation"
                if uses_genx
                else "terminal failure; retry only through replay-safe recovery"
            ),
            proof_class="provider_contract" if uses_genx else "local",
            owner_action_blocker=blocker,
        )


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
        runtime_capability="text_generation_and_document_extraction",
        provider_category="text",
        model_parameter_requirements=("prompt",),
        semantic_qa=True,
        local_operations=("document_extract_text",),
    ),
    WorkerSpec(
        worker_class="research",
        version="1.1.0",
        factory="workers.research.worker.ResearchWorker",
        operations=(
            "research_report", "competitor_research", "market_research", "website_research",
            "multi_source_research", "fact_extraction_research",
        ),
        qa_profile="research",
        description="Web-assisted general, competitor, market, website, multi-source and fact research with explicit citations",
        input_suffixes=(),
        requires_genx=True,
        runtime_capability="session_text_generation",
        provider_category="text",
        tool_requirements=("web_search", "idempotent_session_message"),
        model_parameter_requirements=("system_prompt", "message", "tools"),
        semantic_qa=True,
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
        runtime_capability="text_translation",
        provider_category="text",
        model_parameter_requirements=("prompt",),
        semantic_qa=True,
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
        runtime_capability="speech_to_text",
        provider_category="audio",
        tool_requirements=("file_upload", "file_cleanup"),
        model_parameter_requirements=("audio_file_or_url",),
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
        runtime_capability="bounded_media_transform",
        tool_requirements=("ffmpeg", "ffprobe", "pillow"),
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
        runtime_capability="text_generation",
        provider_category="text",
        model_parameter_requirements=("prompt",),
        semantic_qa=True,
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
        runtime_capability="text_generation",
        provider_category="text",
        model_parameter_requirements=("prompt",),
        semantic_qa=True,
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
        runtime_capability="text_generation",
        provider_category="text",
        model_parameter_requirements=("prompt",),
        semantic_qa=True,
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
        runtime_capability="isolated_ai_coding_sandbox",
        provider_category="text",
        tool_requirements=("sandbox_broker", "aider"),
        model_parameter_requirements=("scoped_proxy_prompt",),
        external_side_effect="isolated_sandbox_write",
        semantic_qa=True,
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
        runtime_capability="isolated_ai_coding_sandbox",
        provider_category="text",
        tool_requirements=("sandbox_broker", "openhands"),
        model_parameter_requirements=("scoped_proxy_prompt",),
        external_side_effect="isolated_sandbox_write",
        semantic_qa=True,
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
        runtime_capability="isolated_ci_sandbox",
        tool_requirements=("sandbox_broker",),
        external_side_effect="isolated_sandbox_write",
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
    WorkerSpec(
        worker_class="image_product",
        version="1.0.0",
        factory="workers.image_product.worker.ImageProductWorker",
        operations=("image_generate_product_asset", "image_edit_product_asset"),
        qa_profile="media",
        description="Original rights-safe commercial image asset generation/editing using task-specific live GenX routing",
        input_suffixes=(".jpg", ".jpeg", ".png", ".webp"),
        requires_genx=True,
        runtime_capability="image_generation_or_editing",
        provider_category="image",
        tool_requirements=("file_upload", "file_cleanup"),
        model_parameter_requirements=("prompt", "dimensions", "source_image_when_editing"),
        semantic_qa=True,
    ),
    WorkerSpec(
        worker_class="ocr",
        version="1.0.0",
        factory="workers.ocr.worker.OCRWorker",
        operations=("ocr_document",),
        qa_profile="transcript",
        description="Bounded local OCR for scanned PDFs and common image formats using Tesseract and Poppler",
        input_suffixes=(".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"),
        runtime_commands=("tesseract", "pdftoppm"),
        runtime_capability="local_scanned_document_ocr",
        tool_requirements=("tesseract", "pdftoppm"),
    ),
    WorkerSpec(
        worker_class="intelligence",
        version="1.0.0",
        factory="workers.intelligence.worker.IntelligenceWorker",
        operations=(
            "intelligence_chat", "intelligence_reason", "intelligence_qa",
            "intelligence_summarize", "intelligence_rewrite", "intelligence_analyze",
        ),
        qa_profile="transcript",
        description="General chat, reasoning, question answering, summarization, rewriting, and analysis through economic GenX text routing",
        requires_genx=True,
        runtime_capability="general_text_intelligence",
        provider_category="text",
        model_parameter_requirements=("prompt",),
        semantic_qa=True,
    ),
    WorkerSpec(
        worker_class="structured_semantic",
        version="1.0.0",
        factory="workers.structured_semantic.worker.StructuredSemanticWorker",
        operations=("structured_json_generate", "classify_text", "extract_structured_facts"),
        qa_profile="tabular",
        description="Schema-oriented JSON generation, text classification, and structured fact extraction with deterministic JSON reopen QA",
        requires_genx=True,
        runtime_capability="structured_semantic_text_generation",
        provider_category="text",
        model_parameter_requirements=("prompt",),
        semantic_qa=True,
    ),
    WorkerSpec(
        worker_class="vision",
        version="1.0.0",
        factory="workers.vision.worker.VisionWorker",
        operations=("vision_understand", "vision_ocr", "vision_qa"),
        qa_profile="transcript",
        description="Authorized image understanding, OCR, and visual question answering using dynamically discovered multimodal text models",
        input_suffixes=(".jpg", ".jpeg", ".png", ".webp"),
        requires_genx=True,
        runtime_capability="multimodal_image_understanding",
        provider_category="text",
        tool_requirements=("file_upload", "file_cleanup"),
        model_parameter_requirements=("prompt", "source_image"),
        semantic_qa=True,
    ),
    WorkerSpec(
        worker_class="generated_media",
        version="1.0.0",
        factory="workers.generated_media.worker.GeneratedMediaWorker",
        operations=("voice_generate", "audio_generate", "music_generate", "video_generate", "image_to_video"),
        qa_profile="media",
        description="Original rights-safe voice, audio, music, video, and image-to-video generation through dynamic GenX media routing",
        input_suffixes=(".jpg", ".jpeg", ".png", ".webp"),
        requires_genx=True,
        runtime_commands=("ffmpeg", "ffprobe"),
        runtime_capability="generated_audio_voice_music_video",
        provider_category="media",
        tool_requirements=("ffprobe", "file_upload_when_source_required", "file_cleanup"),
        model_parameter_requirements=("prompt", "duration", "voice", "language", "source_image_when_required"),
        semantic_qa=True,
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


def operation_contract(operation: str) -> OperationContract:
    return operation_spec(operation).contract(operation)


def all_operation_contracts() -> tuple[OperationContract, ...]:
    return tuple(operation_contract(operation) for operation in registered_operations())


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
            "operation_contracts": [
                {
                    **contract.__dict__,
                    "required_asset_types": list(contract.required_asset_types),
                    "tool_requirements": list(contract.tool_requirements),
                    "model_parameter_requirements": list(contract.model_parameter_requirements),
                    "artifact_requirements": list(contract.artifact_requirements),
                }
                for contract in (spec.contract(operation) for operation in spec.operations)
            ],
        }
        for spec in _SPECS
    ]


def capability_coverage(*, repository_root: Path | None = None) -> dict[str, object]:
    """Return proof counts derived entirely from the live registry.

    The dispatch probe deliberately uses an unsupported operation.  Every worker
    must fail before a paid provider, network, sandbox, or marketplace boundary.
    This exercises the real worker entrypoint while keeping CI commercially inert.
    """
    from workers.base import WorkRequest, Worker
    from workers.qa.runtime import SUPPORTED_QA_PROFILES

    root = repository_root or Path(__file__).resolve().parents[1]
    tests_root = root / "tests"
    test_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in tests_root.glob("test_*.py")
    ) if tests_root.is_dir() else ""
    rows = []
    for contract in all_operation_contracts():
        spec = worker_spec(contract.worker_class)
        errors: list[str] = []
        try:
            worker = spec.build()
            if worker.worker_class != spec.worker_class:
                errors.append("worker_class_mismatch")
            if worker.__class__.execute is Worker.execute or inspect.isabstract(worker.__class__):
                errors.append("missing_execute_implementation")
            probe = worker.execute(WorkRequest(
                job_id="registry-proof-no-job",
                worker_id="registry-proof-no-worker",
                workspace=root / ".registry-proof-never-written",
                inputs={"operation": "__registry_proof_unsupported__"},
            ))
            if probe.ok or not probe.error:
                errors.append("dispatch_not_fail_closed")
        except Exception as exc:  # a proof crash is a failure, never a pass
            errors.append(f"dispatch_probe_crashed:{exc.__class__.__name__}")
        if contract.qa_profile not in SUPPORTED_QA_PROFILES:
            errors.append("qa_profile_unsupported")
        if contract.operation not in test_text:
            errors.append("operation_missing_test_evidence")
        required_values = (
            contract.input_contract, contract.runtime_capability, contract.provider_category,
            contract.cost_policy, contract.output_contract, contract.artifact_requirements,
            contract.qa_profile, contract.repair_policy, contract.submission_policy,
            contract.failure_policy, contract.proof_class,
        )
        if not all(required_values):
            errors.append("incomplete_operation_contract")
        rows.append({
            "operation": contract.operation,
            "worker_class": contract.worker_class,
            "status": "PASS" if not errors else "FAIL",
            "ready": not contract.owner_action_blocker and not errors,
            "owner_action_blocker": contract.owner_action_blocker,
            "errors": errors,
        })

    def count(predicate) -> int:
        return sum(1 for contract in all_operation_contracts() if predicate(contract))

    total = len(rows)
    failed = [row for row in rows if row["status"] != "PASS"]
    summary = {
        "TOTAL_REGISTERED_OPERATIONS": total,
        "OPERATIONS_WITH_WORKERS": total - sum("missing_execute_implementation" in row["errors"] for row in rows),
        "OPERATIONS_WITH_INPUT_CONTRACT": count(lambda row: bool(row.input_contract)),
        "OPERATIONS_WITH_OUTPUT_CONTRACT": count(lambda row: bool(row.output_contract and row.artifact_requirements)),
        "OPERATIONS_WITH_QA": count(lambda row: row.qa_profile in SUPPORTED_QA_PROFILES),
        "OPERATIONS_WITH_COST_POLICY": count(lambda row: bool(row.cost_policy)),
        "OPERATIONS_WITH_FAILURE_POLICY": count(lambda row: bool(row.failure_policy)),
        "OPERATIONS_WITH_TEST_COVERAGE": total - sum("operation_missing_test_evidence" in row["errors"] for row in rows),
        "OPERATIONS_BLOCKED_BY_EXTERNAL_OWNER_ACTION": count(lambda row: bool(row.owner_action_blocker)),
        "OPERATIONS_READY": sum(bool(row["ready"]) for row in rows),
    }
    return {"status": "PASS" if not failed else "FAIL", "summary": summary, "operations": rows}
