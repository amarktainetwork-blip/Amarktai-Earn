from __future__ import annotations

import json
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
