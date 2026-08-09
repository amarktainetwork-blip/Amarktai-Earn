from __future__ import annotations

import json
import base64
import binascii
from urllib.parse import unquote_to_bytes
from typing import Any


def _walk_text(payload: Any, depth: int = 0) -> str:
    if depth > 5:
        return ""
    if isinstance(payload, str):
        value = payload.strip()
        if value and not value.lower().startswith(("http://", "https://")):
            return value
        return ""
    if isinstance(payload, list):
        for item in payload:
            found = _walk_text(item, depth + 1)
            if found:
                return found
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("text", "content", "output_text", "transcript", "translation", "answer"):
        if key in payload:
            found = _walk_text(payload[key], depth + 1)
            if found:
                return found
    for key in ("result", "output", "data", "message", "response", "choices"):
        if key in payload:
            found = _walk_text(payload[key], depth + 1)
            if found:
                return found
    return ""


def extract_text(payload: Any) -> str:
    return _walk_text(payload)


def _message_identity(message: dict[str, Any], key: str) -> str:
    value = message.get(key)
    if value:
        return str(value)
    metadata = message.get("metadata")
    if isinstance(metadata, dict) and metadata.get(key):
        return str(metadata[key])
    return ""


def session_assistant_job_ids(history: Any) -> list[str]:
    """Return distinct job identities from assistant messages only."""
    if not isinstance(history, dict) or not isinstance(history.get("messages"), list):
        return []
    result = []
    for message in history["messages"]:
        if not isinstance(message, dict) or str(message.get("role", "")).lower() != "assistant":
            continue
        job_id = _message_identity(message, "job_id")
        if job_id and job_id not in result:
            result.append(job_id)
    return result


def extract_session_assistant_text(
    history: Any,
    *,
    job_id: str | None = None,
    message_id: str | None = None,
) -> str:
    """Extract only assistant output, preferring a matching asynchronous job."""
    if not isinstance(history, dict) or not isinstance(history.get("messages"), list):
        return ""
    assistants = [
        message
        for message in history["messages"]
        if isinstance(message, dict) and str(message.get("role", "")).lower() == "assistant"
    ]
    if job_id:
        matching = [message for message in assistants if _message_identity(message, "job_id") == str(job_id)]
        if matching:
            assistants = matching
    elif message_id:
        matching = [message for message in assistants if _message_identity(message, "message_id") == str(message_id)]
        if matching:
            assistants = matching
    for assistant in reversed(assistants):
        found = _walk_text(assistant.get("content"))
        if found:
            return found
    return ""


def decode_text_result_url(value: Any, *, max_bytes: int = 1024 * 1024) -> str:
    """Decode bounded inline plain-text results without fetching arbitrary URLs."""
    if not isinstance(value, str) or not value.startswith(("data:", "data/")) or "," not in value:
        return ""
    header, encoded = value[5:].split(",", 1) if value.startswith("data:") else value[5:].split(",", 1)
    if value.startswith("data/"):
        header = "plain" + (";" + header.split(";", 1)[1] if ";" in header else "")
    media_parts = header.split(";") if header else []
    media_type = media_parts[0].lower() if media_parts else "text/plain"
    if media_type not in {"plain", "text/plain"}:
        return ""
    try:
        if any(part.lower() == "base64" for part in media_parts[1:]):
            if len(encoded) > ((max_bytes + 2) // 3) * 4 + 8:
                return ""
            raw = base64.b64decode(encoded, validate=True)
        else:
            raw = unquote_to_bytes(encoded)
    except (ValueError, binascii.Error):
        return ""
    if len(raw) > max_bytes:
        return ""
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def extract_session_sources(payload: Any) -> list[str]:
    urls: list[str] = []

    def visit(value: Any, depth: int = 0):
        if depth > 7:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in {"url", "source_url", "citation_url"} and isinstance(child, str) and child.startswith("https://"):
                    urls.append(child)
                else:
                    visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)

    visit(payload)
    return list(dict.fromkeys(urls))


def json_object(text: str) -> dict[str, Any] | None:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(value[start : end + 1])
        except (ValueError, TypeError):
            return None
    return parsed if isinstance(parsed, dict) else None
