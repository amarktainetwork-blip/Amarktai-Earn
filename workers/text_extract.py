from __future__ import annotations

from pathlib import Path


class TextExtractionError(RuntimeError):
    pass


def extract_text(path: Path, *, max_chars: int = 400_000) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8", errors="replace")
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise TextExtractionError("pypdf is not installed") from exc
        reader = PdfReader(str(path))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    elif suffix == ".docx":
        try:
            from docx import Document
        except ImportError as exc:
            raise TextExtractionError("python-docx is not installed") from exc
        document = Document(str(path))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    else:
        raise TextExtractionError(f"unsupported document type: {suffix or 'none'}")
    text = text.replace("\x00", "").strip()
    if not text:
        raise TextExtractionError("document contained no extractable text")
    if len(text) > max_chars:
        raise TextExtractionError("document text exceeds configured extraction limit")
    return text
