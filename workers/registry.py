from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module


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

    def build(self):
        module_name, attr = self.factory.rsplit(".", 1)
        factory = getattr(import_module(module_name), attr)
        return factory()


_SPECS: tuple[WorkerSpec, ...] = (
    WorkerSpec(
        worker_class="structured_data",
        version="1.0.0",
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
        }
        for spec in _SPECS
    ]
