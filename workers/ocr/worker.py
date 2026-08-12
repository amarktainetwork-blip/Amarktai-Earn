from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from workers.base import WorkRequest, WorkResult, Worker


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def _tesseract(source: Path, *, language: str, timeout: int) -> str:
    result = subprocess.run(
        ["tesseract", str(source), "stdout", "-l", language, "--psm", "3"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("tesseract OCR failed")
    return result.stdout.strip()


class OCRWorker(Worker):
    worker_class = "ocr"

    def execute(self, request: WorkRequest) -> WorkResult:
        if request.inputs.get("operation") != "ocr_document":
            return WorkResult(ok=False, error="unsupported OCR operation")
        source = Path(str(request.inputs.get("source") or ""))
        if not source.is_file():
            return WorkResult(ok=False, error="OCR requires a verified source file")
        language = str(request.inputs.get("language") or "eng").strip().casefold()
        if language != "eng":
            return WorkResult(ok=False, error="local OCR currently supports the installed English language pack only")
        max_pages = max(1, min(int(request.inputs.get("max_pages") or os.getenv("OCR_MAX_PAGES", "50")), 100))
        timeout = max(5, min(int(os.getenv("OCR_PAGE_TIMEOUT_SECONDS", "60")), 300))
        suffix = source.suffix.casefold()
        texts: list[str] = []
        page_count = 0
        try:
            if suffix in _IMAGE_SUFFIXES:
                texts.append(_tesseract(source, language=language, timeout=timeout))
                page_count = 1
            elif suffix == ".pdf":
                with tempfile.TemporaryDirectory(prefix="amarktai-ocr-") as temp:
                    prefix = Path(temp) / "page"
                    rendered = subprocess.run(
                        [
                            "pdftoppm", "-png", "-r", "200", "-f", "1", "-l", str(max_pages),
                            str(source), str(prefix),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=max(timeout, timeout * max_pages),
                        check=False,
                    )
                    if rendered.returncode != 0:
                        return WorkResult(ok=False, error="PDF rasterization for OCR failed")
                    pages = sorted(Path(temp).glob("page-*.png"))[:max_pages]
                    if not pages:
                        return WorkResult(ok=False, error="PDF OCR produced no rasterized pages")
                    for page in pages:
                        texts.append(_tesseract(page, language=language, timeout=timeout))
                    page_count = len(pages)
            else:
                return WorkResult(ok=False, error="OCR supports PDF and common image formats")
        except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))

        combined = "\n\n".join(text for text in texts if text).strip()
        if not combined:
            return WorkResult(ok=False, error="OCR completed but extracted no text")
        request.workspace.mkdir(parents=True, exist_ok=True)
        target = request.workspace / "ocr-document.txt"
        target.write_text(combined + "\n", encoding="utf-8")
        return WorkResult(
            ok=True,
            artifacts=[target],
            evidence={
                "operation": "ocr_document",
                "page_count": page_count,
                "output_chars": len(combined),
                "language": language,
                "local_ocr": True,
                "runtime": "tesseract+poppler",
            },
        )
