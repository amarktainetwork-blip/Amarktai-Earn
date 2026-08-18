from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from workers.base import WorkRequest, WorkResult, Worker
from workers.vision.worker import VisionWorker


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def _offhost_page(request: WorkRequest, source: Path, page: int) -> WorkResult:
    """Run recognition through the canonical paid-provider vision worker."""
    child = WorkRequest(
        job_id=request.job_id,
        workspace=request.workspace / f"provider-page-{page}",
        inputs={
            **request.inputs,
            "operation": "vision_ocr",
            "source": str(source),
            "source_authorized": True,
        },
        worker_id=request.worker_id,
        execution_id=request.execution_id,
        attempt=request.attempt,
    )
    return VisionWorker().execute(child)


class OCRWorker(Worker):
    """Scanned-document OCR without a neural runtime on the Webdock host."""

    worker_class = "ocr"

    def execute(self, request: WorkRequest) -> WorkResult:
        if request.inputs.get("operation") != "ocr_document":
            return WorkResult(ok=False, error="unsupported OCR operation")
        source = Path(str(request.inputs.get("source") or ""))
        if not source.is_file():
            return WorkResult(ok=False, error="OCR requires a verified source file")
        if request.inputs.get("source_authorized") is not True:
            return WorkResult(ok=False, error="OCR source authorization confirmation is required")
        language = str(request.inputs.get("language") or "eng").strip().casefold()
        if language != "eng":
            return WorkResult(ok=False, error="off-host OCR currently supports English requests only")

        max_pages = max(1, min(int(request.inputs.get("max_pages") or os.getenv("OCR_MAX_PAGES", "10")), 25))
        timeout = max(5, min(int(os.getenv("OCR_PDF_RENDER_TIMEOUT_SECONDS", "120")), 300))
        suffix = source.suffix.casefold()
        texts: list[str] = []
        provider_evidence: list[dict] = []

        try:
            if suffix in _IMAGE_SUFFIXES:
                pages = [source]
                temp_context = None
            elif suffix == ".pdf":
                temp_context = tempfile.TemporaryDirectory(prefix="amarktai-ocr-render-")
                prefix = Path(temp_context.name) / "page"
                rendered = subprocess.run(
                    [
                        "pdftoppm", "-png", "-r", "200", "-f", "1", "-l", str(max_pages),
                        str(source), str(prefix),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                if rendered.returncode != 0:
                    temp_context.cleanup()
                    return WorkResult(ok=False, error="deterministic PDF rasterization for OCR failed")
                pages = sorted(Path(temp_context.name).glob("page-*.png"))[:max_pages]
                if not pages:
                    temp_context.cleanup()
                    return WorkResult(ok=False, error="PDF OCR produced no rasterized pages")
            else:
                return WorkResult(ok=False, error="OCR supports PDF and common image formats")

            try:
                for page_number, page in enumerate(pages, start=1):
                    result = _offhost_page(request, page, page_number)
                    if not result.ok:
                        return WorkResult(
                            ok=False,
                            error=result.error or "READY_FOR_CREDENTIAL: off-host OCR provider unavailable",
                            evidence={"readiness": "READY_FOR_CREDENTIAL", "local_neural_runtime": False},
                        )
                    text = result.artifacts[0].read_text(encoding="utf-8").strip()
                    texts.append(text)
                    provider_evidence.append(dict(result.evidence))
            finally:
                if temp_context is not None:
                    temp_context.cleanup()
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))

        combined = "\n\n".join(text for text in texts if text).strip()
        if not combined:
            return WorkResult(ok=False, error="off-host OCR completed but extracted no text")
        request.workspace.mkdir(parents=True, exist_ok=True)
        target = request.workspace / "ocr-document.txt"
        target.write_text(combined + "\n", encoding="utf-8")
        return WorkResult(
            ok=True,
            artifacts=[target],
            evidence={
                "operation": "ocr_document",
                "page_count": len(texts),
                "output_chars": len(combined),
                "language": language,
                "local_neural_runtime": False,
                "runtime": "offhost_genx_vision+local_poppler",
                "provider_calls": provider_evidence,
            },
        )
