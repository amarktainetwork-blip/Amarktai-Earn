from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class RemoteAssetSafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteAssetRef:
    external_id: str
    name: str
    url: str = ""
    size_bytes: int = 0
    mime_type: str = ""
    source_kind: str = "job_attachment"
    semantic_role: str = ""


SUPPORTED_SOURCE_SUFFIXES = {
    ".json", ".csv", ".xlsx", ".pdf", ".docx", ".pptx", ".txt", ".md", ".html", ".htm",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4", ".mov", ".webm",
}
DEFAULT_MAX_SOURCE_BYTES = 25 * 1024 * 1024


def positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def remote_asset_id(*values: Any) -> str:
    material = "|".join(str(value or "") for value in values)
    return f"agentgigs:{hashlib.sha256(material.encode('utf-8', errors='ignore')).hexdigest()[:48]}"


def _row_to_ref(row: dict[str, Any], *, source_kind: str, message_id: str = "") -> RemoteAssetRef | None:
    name = str(row.get("file_name") or row.get("attachment_name") or row.get("name") or "").strip()
    url = str(row.get("download_url") or row.get("attachment_url") or row.get("url") or "").strip()
    storage_path = str(row.get("file_path") or row.get("storage_path") or row.get("path") or "").strip()
    remote_row_id = str(row.get("id") or "").strip()
    if not name and storage_path:
        name = Path(storage_path).name
    if not name and url:
        name = Path(urlparse(url).path).name
    if not name or not url:
        return None
    return RemoteAssetRef(
        external_id=remote_asset_id(source_kind, message_id, remote_row_id, storage_path, url.split("?", 1)[0], name),
        name=name,
        url=url,
        size_bytes=positive_int(row.get("file_size") or row.get("attachment_size") or row.get("size")),
        mime_type=str(row.get("mime_type") or row.get("content_type") or "").strip(),
        source_kind=source_kind,
        semantic_role=str(row.get("semantic_role") or row.get("role") or "").strip(),
    )


def extract_source_asset_refs(details: dict[str, Any] | None, messages: list[dict[str, Any]] | None) -> list[RemoteAssetRef]:
    refs: list[RemoteAssetRef] = []
    details = details if isinstance(details, dict) else {}
    job = details.get("job") if isinstance(details.get("job"), dict) else {}
    for container in (details.get("attachments"), job.get("attachments")):
        if not isinstance(container, list):
            continue
        for row in container:
            if isinstance(row, dict):
                ref = _row_to_ref(row, source_kind="job_attachment")
                if ref:
                    refs.append(ref)

    for row in messages or []:
        if not isinstance(row, dict):
            continue
        if not (row.get("attachment_url") or row.get("download_url")):
            continue
        ref = _row_to_ref(
            row,
            source_kind="message_attachment",
            message_id=str(row.get("id") or row.get("message_id") or ""),
        )
        if ref:
            refs.append(ref)

    deduped: list[RemoteAssetRef] = []
    seen: set[str] = set()
    for ref in refs:
        if ref.external_id in seen:
            continue
        seen.add(ref.external_id)
        deduped.append(ref)
    return deduped


def safe_filename(name: str, external_id: str) -> str:
    raw = Path(str(name)).name.strip().replace("\x00", "")
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)[:160]
    if not raw or raw in {".", ".."}:
        raw = f"source-{external_id.rsplit(':', 1)[-1][:12]}"
    return raw


def supported_source_name(name: str) -> bool:
    return Path(name).suffix.casefold() in SUPPORTED_SOURCE_SUFFIXES


def assert_public_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RemoteAssetSafetyError("remote asset URL must be credential-free HTTPS")
    if parsed.port not in (None, 443):
        raise RemoteAssetSafetyError("remote asset URL must use HTTPS port 443")
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".local"):
        raise RemoteAssetSafetyError("remote asset URL resolves to a local hostname")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise RemoteAssetSafetyError("remote asset URL uses a non-public address")
        return
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise RemoteAssetSafetyError("remote asset hostname could not be resolved") from exc
    addresses = {item[4][0] for item in infos if item and item[4]}
    if not addresses:
        raise RemoteAssetSafetyError("remote asset hostname resolved to no addresses")
    for address in addresses:
        try:
            parsed_ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise RemoteAssetSafetyError("remote asset hostname returned an invalid address") from exc
        if not parsed_ip.is_global:
            raise RemoteAssetSafetyError("remote asset hostname resolved to a non-public address")
