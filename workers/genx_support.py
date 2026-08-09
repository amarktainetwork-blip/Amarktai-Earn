from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from control.models import GenXModelCatalog, Job
from gateways.genx.output import extract_session_assistant_text, extract_session_sources, extract_text
from gateways.genx.service import GenXGateway, GenXGatewayError


class GenXWorkerError(RuntimeError):
    pass


def _decimal(value: Any, default: str) -> Decimal:
    try:
        result = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise GenXWorkerError("invalid GenX credit envelope") from exc
    if result <= 0:
        raise GenXWorkerError("GenX credit envelope must be positive")
    return result


def credit_envelope(job_id, inputs: dict[str, Any]) -> tuple[Decimal, Decimal]:
    try:
        job = Job.objects.select_related("jobscore").get(pk=job_id)
        budget = Decimal(job.jobscore.max_genx_credits)
    except Exception as exc:
        raise GenXWorkerError("job has no persisted GenX credit envelope") from exc
    if budget <= 0:
        raise GenXWorkerError("job has no positive GenX credit budget")
    estimated = _decimal(inputs.get("estimated_genx_credits"), os.getenv("GENX_DEFAULT_ESTIMATED_CREDITS", "0.25"))
    per_call = _decimal(inputs.get("max_genx_call_credits"), os.getenv("GENX_DEFAULT_MAX_CALL_CREDITS", "1.0"))
    return min(estimated, budget), min(per_call, budget)


def _searchable_model_text(row: GenXModelCatalog) -> str:
    return " ".join(
        [row.model_id, row.category, row.provider, json.dumps(row.model_payload, sort_keys=True, default=str)]
    ).casefold()


def catalog_supports(*keywords: str, fallback_category: str | None = None) -> bool:
    """Return whether the active catalog can satisfy a worker's actual routing contract."""
    rows = list(GenXModelCatalog.objects.filter(active=True))
    wanted = tuple(word.casefold() for word in keywords if word)
    if wanted and any(any(word in _searchable_model_text(row) for word in wanted) for row in rows):
        return True
    return bool(
        fallback_category
        and any(row.category.casefold() == fallback_category.casefold() for row in rows)
    )


def select_specialist(*keywords: str, fallback_category: str | None = None) -> GenXModelCatalog:
    rows = list(GenXModelCatalog.objects.filter(active=True))
    wanted = tuple(word.casefold() for word in keywords if word)
    matched = [row for row in rows if wanted and any(word in _searchable_model_text(row) for word in wanted)]
    if matched:
        matched.sort(key=lambda row: (row.price_hint is None, row.price_hint or Decimal("999999999"), row.model_id))
        return matched[0]
    if fallback_category:
        fallback = [row for row in rows if row.category.casefold() == fallback_category.casefold()]
        if fallback:
            fallback.sort(key=lambda row: (row.price_hint is None, row.price_hint or Decimal("999999999"), row.model_id))
            return fallback[0]
    raise GenXWorkerError(f"no active GenX model matched capability: {', '.join(keywords) or fallback_category}")


def _request_key(request, task_class: str, extra: str = "") -> str:
    material = f"{request.job_id}|{request.worker_id}|{task_class}|{request.attempt}|{extra}"
    return f"worker:{hashlib.sha256(material.encode()).hexdigest()[:48]}"


def _terminal_text(gateway: GenXGateway, call) -> str:
    if call.status != "COMPLETED":
        raise GenXWorkerError(f"GenX call did not complete: {call.status}")
    if not call.external_job_id:
        raise GenXWorkerError("GenX call has no remote job ID")
    payload = gateway.client.job(call.external_job_id)
    text = extract_text(payload)
    if not text:
        try:
            text = extract_text(gateway.client.result(call.external_job_id))
        except Exception:
            text = ""
    if not text:
        try:
            raw = gateway.client.job_file(call.external_job_id, max_bytes=int(os.getenv("GENX_MAX_TEXT_RESULT_BYTES", "8388608")))
            text = raw.decode("utf-8", errors="replace").strip()
        except Exception:
            text = ""
    if not text:
        raise GenXWorkerError("GenX completed without extractable text output")
    return text


def generate_text(request, *, prompt: str, task_class: str, capability_keywords: tuple[str, ...] = ()) -> tuple[str, Any]:
    gateway = GenXGateway()
    estimated, call_limit = credit_envelope(request.job_id, request.inputs)
    selected = select_specialist(*capability_keywords, fallback_category="text") if capability_keywords else None
    call = gateway.run(
        job_id=request.job_id,
        worker_id=request.worker_id,
        category=selected.category if selected else "text",
        task_class=task_class,
        params={"prompt": prompt},
        estimated_credits=estimated,
        max_allowed_credits=call_limit,
        request_key=_request_key(request, task_class, hashlib.sha256(prompt.encode()).hexdigest()[:12]),
        preferred_model=selected.model_id if selected else None,
        wait_timeout_seconds=int(os.getenv("GENX_WORKER_TIMEOUT_SECONDS", "240")),
    )
    return _terminal_text(gateway, call), call


def research_with_web(request, *, query: str, requirements: str = "") -> tuple[str, list[str], Any]:
    gateway = GenXGateway()
    estimated, call_limit = credit_envelope(request.job_id, request.inputs)
    system_prompt = (
        "You are a research worker. Use web search for current evidence. "
        "Produce a concise professional report. Every material factual claim must be supported by an HTTPS source URL. "
        "End with a Sources section containing the URLs you actually used. Do not invent sources."
    )
    message = f"Research task: {query}\n\nRequirements:\n{requirements or 'Provide the strongest evidence and note uncertainty.'}"
    call, response = gateway.run_session(
        job_id=request.job_id,
        worker_id=request.worker_id,
        task_class="research_web",
        system_prompt=system_prompt,
        message=message,
        estimated_credits=estimated,
        max_allowed_credits=call_limit,
        request_key=_request_key(request, "research_web", hashlib.sha256(message.encode()).hexdigest()[:12]),
        tools=[{"type": "web_search"}],
    )
    text = str(response.get("assistant_text") or "") if isinstance(response, dict) else ""
    if not text:
        text = extract_text(response)
    session_id = str((call.requested_metadata or {}).get("session_id") or "")
    if not text and session_id:
        history = gateway.client.session_messages(session_id)
        text = extract_session_assistant_text(
            history,
            job_id=str((call.requested_metadata or {}).get("remote_job_id") or "") or None,
        )
    if not text:
        raise GenXWorkerError("GenX research session returned no report text")
    source_payload = response.get("session_history", response) if isinstance(response, dict) else response
    sources = extract_session_sources(source_payload)
    if not sources:
        import re
        sources = list(dict.fromkeys(re.findall(r"https://[^\s)\]>]+", text)))
    return text, sources, call


def _parameter_names(payload: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            names.add(str(key).casefold())
            if str(key).casefold() in {"name", "key", "field", "parameter"} and isinstance(value, str):
                names.add(value.casefold())
            names.update(_parameter_names(value))
    elif isinstance(payload, list):
        for value in payload:
            names.update(_parameter_names(value))
    return names


def transcribe_media(request, source: Path) -> tuple[str, Any]:
    gateway = GenXGateway()
    selected = select_specialist("transcription", "transcribe", "speech to text")
    uploaded = gateway.client.upload_file(source)
    file_id = str(uploaded.get("file_id") or uploaded.get("id") or "")
    file_url = str(uploaded.get("url") or uploaded.get("download_url") or "")
    names = _parameter_names(selected.model_payload)
    params: dict[str, Any] = {}
    for candidate in ("audio_file_id", "input_file_id", "file_id", "asset_id"):
        if candidate in names and file_id:
            params[candidate] = file_id
            break
    if not params:
        for candidate in ("audio_url", "input_url", "file_url", "media_url", "url"):
            if candidate in names and file_url:
                params[candidate] = file_url
                break
    if not params:
        raise GenXWorkerError("transcription model schema exposes no recognized uploaded-file input")
    estimated, call_limit = credit_envelope(request.job_id, request.inputs)
    call = gateway.run(
        job_id=request.job_id,
        worker_id=request.worker_id,
        category=selected.category,
        task_class="transcription",
        params=params,
        estimated_credits=estimated,
        max_allowed_credits=call_limit,
        request_key=_request_key(request, "transcription", file_id or source.name),
        preferred_model=selected.model_id,
        wait_timeout_seconds=int(os.getenv("GENX_TRANSCRIPTION_TIMEOUT_SECONDS", "600")),
    )
    text = _terminal_text(gateway, call)
    if file_id and call.status == "COMPLETED":
        try:
            gateway.client.delete_file(file_id)
        except Exception:
            # Cleanup failure must not invalidate a completed paid transcription.
            pass
    return text, call
