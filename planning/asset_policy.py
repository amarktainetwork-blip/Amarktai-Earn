from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


APPROVED_ROLES = {
    "brief",
    "source",
    "reference",
    "data",
    "media",
    "repository",
    "expected_output_reference",
    "other-approved",
}

EXTENSION_MIME = {
    ".json": {"application/json", "text/json"},
    ".csv": {"text/csv", "text/plain"},
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/plain"},
    ".html": {"text/html"},
    ".htm": {"text/html"},
    ".pdf": {"application/pdf"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".png": {"image/png"},
    ".webp": {"image/webp"},
    ".mp3": {"audio/mpeg"},
    ".wav": {"audio/wav", "audio/x-wav"},
    ".m4a": {"audio/mp4"},
    ".ogg": {"audio/ogg"},
    ".flac": {"audio/flac"},
    ".mp4": {"video/mp4"},
    ".mov": {"video/quicktime"},
    ".webm": {"video/webm"},
}


class AssetPolicyError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AssetInspection:
    detected_mime_type: str
    archive_inspected: bool


def safe_asset_name(name: str) -> str:
    raw = Path(str(name)).name.strip().replace("\x00", "")
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw)[:180].strip()
    if not safe or safe in {".", ".."}:
        raise AssetPolicyError("ASSET_FILENAME_UNSAFE")
    return safe


def validate_role(role: str) -> str:
    normalized = str(role or "").strip().casefold().replace(" ", "-")
    normalized = {
        "expected-output-reference": "expected_output_reference",
        "other_approved": "other-approved",
    }.get(normalized, normalized)
    if normalized not in APPROVED_ROLES:
        raise AssetPolicyError("ASSET_ROLE_NOT_APPROVED")
    return normalized


def _inspect_office_archive(path: Path, suffix: str) -> AssetInspection:
    expected = {
        ".docx": ("word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ".xlsx": ("xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ".pptx": ("ppt/", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    }[suffix]
    max_members = max(1, int(os.getenv("JOB_ASSET_MAX_ARCHIVE_MEMBERS", "5000")))
    max_unpacked = max(1, int(os.getenv("JOB_ASSET_MAX_ARCHIVE_UNPACKED_BYTES", str(200 * 1024 * 1024))))
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > max_members:
                raise AssetPolicyError("ASSET_ARCHIVE_MEMBER_LIMIT")
            total = 0
            names = set()
            for member in members:
                pure = PurePosixPath(member.filename)
                if pure.is_absolute() or ".." in pure.parts or "\x00" in member.filename:
                    raise AssetPolicyError("ASSET_ARCHIVE_PATH_UNSAFE")
                mode = (member.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise AssetPolicyError("ASSET_ARCHIVE_SYMLINK_BLOCKED")
                total += max(0, member.file_size)
                if total > max_unpacked:
                    raise AssetPolicyError("ASSET_ARCHIVE_UNPACKED_LIMIT")
                names.add(member.filename.casefold())
            if "[content_types].xml" not in names or not any(name.startswith(expected[0]) for name in names):
                raise AssetPolicyError("ASSET_OFFICE_STRUCTURE_INVALID")
            if any(name.endswith(("vbaproject.bin", ".exe", ".dll", ".com", ".scr")) for name in names):
                raise AssetPolicyError("ASSET_ACTIVE_CONTENT_BLOCKED")
    except zipfile.BadZipFile as exc:
        raise AssetPolicyError("ASSET_ARCHIVE_INVALID") from exc
    return AssetInspection(expected[1], True)


def inspect_asset(path: Path) -> AssetInspection:
    suffix = path.suffix.casefold()
    if suffix not in EXTENSION_MIME:
        raise AssetPolicyError("ASSET_EXTENSION_UNSUPPORTED")
    with path.open("rb") as handle:
        head = handle.read(8192)
    if b"\x00" in head and suffix in {".json", ".csv", ".txt", ".md", ".html", ".htm"}:
        raise AssetPolicyError("ASSET_TEXT_CONTAINS_NUL")
    if suffix == ".pdf":
        if not head.startswith(b"%PDF-"):
            raise AssetPolicyError("ASSET_MIME_EXTENSION_MISMATCH")
        return AssetInspection("application/pdf", False)
    if suffix in {".docx", ".xlsx", ".pptx"}:
        return _inspect_office_archive(path, suffix)
    if suffix in {".jpg", ".jpeg"}:
        detected = "image/jpeg" if head.startswith(b"\xff\xd8\xff") else ""
    elif suffix == ".png":
        detected = "image/png" if head.startswith(b"\x89PNG\r\n\x1a\n") else ""
    elif suffix == ".webp":
        detected = "image/webp" if head.startswith(b"RIFF") and head[8:12] == b"WEBP" else ""
    elif suffix == ".wav":
        detected = "audio/wav" if head.startswith(b"RIFF") and head[8:12] == b"WAVE" else ""
    elif suffix == ".flac":
        detected = "audio/flac" if head.startswith(b"fLaC") else ""
    elif suffix == ".ogg":
        detected = "audio/ogg" if head.startswith(b"OggS") else ""
    elif suffix == ".mp3":
        detected = "audio/mpeg" if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0) else ""
    elif suffix in {".mp4", ".m4a", ".mov"}:
        detected = ({".mp4": "video/mp4", ".m4a": "audio/mp4", ".mov": "video/quicktime"}[suffix] if b"ftyp" in head[:32] else "")
    elif suffix == ".webm":
        detected = "video/webm" if head.startswith(b"\x1aE\xdf\xa3") else ""
    elif suffix == ".json":
        import json
        try:
            with path.open("r", encoding="utf-8") as handle:
                json.load(handle)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AssetPolicyError("ASSET_JSON_INVALID") from exc
        detected = "application/json"
    elif suffix == ".csv":
        try:
            sample = head.decode("utf-8-sig")
            list(csv.reader(io.StringIO(sample)))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise AssetPolicyError("ASSET_CSV_INVALID") from exc
        detected = "text/csv"
    else:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AssetPolicyError("ASSET_TEXT_ENCODING_INVALID") from exc
        detected = {".html": "text/html", ".htm": "text/html", ".md": "text/markdown"}.get(suffix, "text/plain")
    if not detected or detected not in EXTENSION_MIME[suffix]:
        raise AssetPolicyError("ASSET_MIME_EXTENSION_MISMATCH")
    return AssetInspection(detected, False)
