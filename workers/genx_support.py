from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from django.utils import timezone

from control.models import AuditEvent, GenXCall, GenXModelCatalog, Job
from gateways.genx.client import GenXError
from gateways.genx.contracts import build_model_params, model_parameter_names
from gateways.genx.output import extract_session_assistant_text, extract_session_sources, extract_text
from gateways.genx.service import GenXGateway


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


def _normalize_catalog_text(value: Any) -> str:
    """Normalize provider metadata spelling without inventing capabilities."""
    return " ".join(str(value).casefold().replace("_", " ").replace("-", " ").split())


def _searchable_model_text(row: GenXModelCatalog) -> str:
    return _normalize_catalog_text(
        " ".join(
            [row.model_id, row.category, row.provider, json.dumps(row.model_payload, sort_keys=True, default=str)]
        )
    )


def catalog_supports(*keywords: str, fallback_category: str | None = None) -> bool:
    """Return whether the active catalog can satisfy a worker's actual routing contract."""
    rows = list(GenXModelCatalog.objects.filter(active=True))
    wanted = tuple(_normalize_catalog_text(word) for word in keywords if word)
    if wanted and any(any(word in _searchable_model_text(row) for word in wanted) for row in rows):
        return True
    return bool(
        fallback_category
        and any(row.category.casefold() == fallback_category.casefold() for row in rows)
    )


def select_specialist(*keywords: str, fallback_category: str | None = None) -> GenXModelCatalog:
    """Legacy catalogue helper for non-production inspection; it does not select a paid model."""
    ids = capability_model_ids(*keywords, fallback_category=fallback_category)
    row = GenXModelCatalog.objects.filter(model_id__in=ids).order_by("model_id").first()
    if row:
        return row
    raise GenXWorkerError(f"no active GenX model matched capability: {', '.join(keywords) or fallback_category}")


def capability_model_ids(*keywords: str, fallback_category: str | None = None) -> list[str]:
    """Filter the live catalogue by capability without making an economic selection."""
    rows = list(GenXModelCatalog.objects.filter(active=True))
    wanted = tuple(_normalize_catalog_text(word) for word in keywords if word)
    matched = [row for row in rows if wanted and any(word in _searchable_model_text(row) for word in wanted)]
    if matched:
        return sorted(row.model_id for row in matched)
    if fallback_category:
        fallback = [row for row in rows if row.category.casefold() == fallback_category.casefold()]
        if fallback:
            return sorted(row.model_id for row in fallback)
    raise GenXWorkerError(f"no active GenX model matched capability: {', '.join(keywords) or fallback_category}")


def _request_key(request, task_class: str, extra: str = "") -> str:
    material = f"{request.job_id}|{request.worker_id}|{task_class}|{request.attempt}|{extra}"
    return f"worker:{hashlib.sha256(material.encode()).hexdigest()[:48]}"


def _confirmed_research_tool_rejection(call: GenXCall | None) -> bool:
    """Return true only for provider-confirmed, zero-cost Web Search compatibility rejection."""
    if call is None or call.task_class != "research_web" or call.status != "FAILED":
        return False
    metadata = call.requested_metadata or {}
    if str(metadata.get("billing_truth") or "") != "NOT_APPLICABLE":
        return False
    event = (
        AuditEvent.objects.filter(
            event_type__in=("genx.session_failed", "genx.session_remote_tool_rejected"),
            metadata__call_id=str(call.id),
        )
        .order_by("-created_at")
        .first()
    )
    if not event:
        return False
    evidence = event.metadata or {}
    try:
        http_status = int(evidence.get("http_status") or 0)
    except (TypeError, ValueError):
        http_status = 0
    if http_status not in {400, 422}:
        return False
    remote_job_id = str(metadata.get("remote_job_id") or "")
    if event.event_type == "genx.session_failed":
        return not remote_job_id and str(evidence.get("phase") or "") == "SEND_MESSAGE"
    return bool(remote_job_id) and str(evidence.get("phase") or "") == "POLL_REMOTE_JOB"


def _remote_failure_http_status(response: dict[str, Any]) -> tuple[int, str, str]:
    remote = response.get("remote_job") if isinstance(response, dict) else None
    if not isinstance(remote, dict) or str(remote.get("status") or "").casefold() != "failed":
        return 0, "", ""
    error = str(remote.get("error") or remote.get("message") or "")
    remote_job_id = str(remote.get("job_id") or remote.get("id") or "")
    raw_status = remote.get("http_status") or remote.get("status_code")
    try:
        status = int(raw_status) if raw_status not in (None, "") else 0
    except (TypeError, ValueError):
        status = 0
    if not status:
        match = re.search(r"\((\d{3})\)\s*$", error)
        status = int(match.group(1)) if match else 0
    return status, error, remote_job_id


def _record_research_remote_tool_rejection(call: GenXCall | None, response: dict[str, Any]) -> bool:
    """Persist a terminal failed GenX job as zero-cost compatibility evidence only when unambiguous."""
    if call is None or call.task_class != "research_web" or call.status != "FAILED":
        return False
    http_status, provider_error, payload_job_id = _remote_failure_http_status(response)
    if http_status not in {400, 422}:
        return False
    metadata = dict(call.requested_metadata or {})
    remote_job_id = str(metadata.get("remote_job_id") or call.external_job_id or "")
    if not remote_job_id or (payload_job_id and payload_job_id != remote_job_id):
        return False
    if Decimal(call.credits or 0) != Decimal("0"):
        return False
    if call.cost_equivalent not in (None, Decimal("0")):
        return False
    if str(metadata.get("billing_truth") or "") == "ACTUAL":
        return False

    metadata.update({
        "billing_truth": "NOT_APPLICABLE",
        "cost_equivalent_truth": "NOT_APPLICABLE",
        "provider_http_status": http_status,
        "provider_error": provider_error,
        "remote_job_id": remote_job_id,
    })
    GenXCall.objects.filter(pk=call.pk).update(
        credits=Decimal("0"),
        cost_equivalent=Decimal("0"),
        error_code=f"PROVIDER_HTTP_{http_status}",
        requested_metadata=metadata,
    )
    call.credits = Decimal("0")
    call.cost_equivalent = Decimal("0")
    call.error_code = f"PROVIDER_HTTP_{http_status}"
    call.requested_metadata = metadata
    AuditEvent.objects.create(
        severity="ERROR",
        event_type="genx.session_remote_tool_rejected",
        actor="genx-gateway",
        metadata={
            "call_id": str(call.id),
            "job_id": str(call.job_id or ""),
            "model": call.model,
            "phase": "POLL_REMOTE_JOB",
            "http_status": http_status,
            "remote_job_id": remote_job_id,
            "provider_error": provider_error,
            "billing_truth": "NOT_APPLICABLE",
        },
    )
    return True


def research_web_model_ids(*, excluded_model_ids: set[str] | None = None) -> list[str]:
    """Resolve Web Search candidates from provider metadata plus recent live session evidence."""
    excluded = {str(value) for value in (excluded_model_ids or set()) if value}
    active_text = capability_model_ids(fallback_category="text")
    try:
        explicit = capability_model_ids("web_search")
    except GenXWorkerError:
        explicit = []

    try:
        supported_hours = max(1, int(os.getenv("GENX_TOOL_SUPPORT_TTL_HOURS", "168")))
        rejected_hours = max(1, int(os.getenv("GENX_TOOL_REJECTION_TTL_HOURS", "24")))
    except ValueError as exc:
        raise GenXWorkerError("invalid GenX tool capability evidence TTL") from exc

    oldest_cutoff = timezone.now() - timedelta(hours=max(supported_hours, rejected_hours))
    latest_observation: dict[str, tuple[str, Any]] = {}
    evidence_calls = (
        GenXCall.objects.filter(
            task_class="research_web",
            model__in=active_text,
            created_at__gte=oldest_cutoff,
        )
        .order_by("-created_at")
    )
    for call in evidence_calls:
        if call.model in latest_observation:
            continue
        if call.status == "COMPLETED":
            latest_observation[call.model] = ("SUPPORTED", call.created_at)
        elif _confirmed_research_tool_rejection(call):
            latest_observation[call.model] = ("REJECTED", call.created_at)

    supported_cutoff = timezone.now() - timedelta(hours=supported_hours)
    rejected_cutoff = timezone.now() - timedelta(hours=rejected_hours)
    known_supported = {
        model
        for model, (state, observed_at) in latest_observation.items()
        if state == "SUPPORTED" and observed_at >= supported_cutoff
    }
    known_rejected = {
        model
        for model, (state, observed_at) in latest_observation.items()
        if state == "REJECTED" and observed_at >= rejected_cutoff
    }

    proven = sorted((known_supported & set(active_text)) - excluded)
    if proven:
        return proven

    preferred = explicit or active_text
    candidates = [model for model in preferred if model not in known_rejected and model not in excluded]
    if candidates:
        return sorted(candidates)

    # Live rejection evidence overrides stale/incorrect catalogue capability text.
    if explicit:
        fallback = [model for model in active_text if model not in known_rejected and model not in excluded]
        if fallback:
            return sorted(fallback)

    raise GenXWorkerError("no active GenX text model remains eligible for web_search after live tool rejection evidence")


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
    job = Job.objects.get(pk=request.job_id)
    eligible = capability_model_ids(*capability_keywords, fallback_category="text") if capability_keywords else None
    call = gateway.run(
        job_id=request.job_id,
        worker_id=request.worker_id,
        category="text",
        task_class=task_class,
        params={"prompt": prompt},
        estimated_credits=estimated,
        max_allowed_credits=call_limit,
        request_key=_request_key(request, task_class, hashlib.sha256(prompt.encode()).hexdigest()[:12]),
        eligible_model_ids=eligible,
        wait_timeout_seconds=int(os.getenv("GENX_WORKER_TIMEOUT_SECONDS", "240")),
        required_quality=Decimal(str(request.inputs.get("minimum_quality", "0.80"))),
        allow_exploration=bool(request.inputs.get("allow_model_exploration", False)),
        economically_fragile=bool(request.inputs.get("economically_fragile", False)),
        expected_revenue=job.reward,
        required_params=("prompt",),
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
    job = Job.objects.get(pk=request.job_id)
    message_digest = hashlib.sha256(message.encode()).hexdigest()[:12]
    try:
        max_negotiation_models = max(1, int(os.getenv("GENX_RESEARCH_TOOL_NEGOTIATION_MAX_MODELS", "6")))
    except ValueError as exc:
        raise GenXWorkerError("invalid GenX research tool negotiation limit") from exc

    excluded: set[str] = set()
    call = None
    response: dict[str, Any] = {}
    last_rejection: Exception | None = None

    for negotiation_attempt in range(1, max_negotiation_models + 1):
        eligible = research_web_model_ids(excluded_model_ids=excluded)
        request_key = _request_key(
            request,
            "research_web",
            f"{message_digest}:web-search-negotiation:{negotiation_attempt}",
        )
        try:
            call, response = gateway.run_session(
                job_id=request.job_id,
                worker_id=request.worker_id,
                task_class="research_web",
                system_prompt=system_prompt,
                message=message,
                estimated_credits=estimated,
                max_allowed_credits=call_limit,
                request_key=request_key,
                tools=[{"type": "web_search"}],
                eligible_model_ids=eligible,
                required_quality=Decimal(str(request.inputs.get("minimum_quality", "0.80"))),
                expected_revenue=job.reward,
                allow_exploration=bool(request.inputs.get("allow_model_exploration", False)),
                economically_fragile=bool(request.inputs.get("economically_fragile", False)),
            )
            if call.status == "FAILED":
                if _record_research_remote_tool_rejection(call, response):
                    excluded.add(call.model)
                    last_rejection = GenXWorkerError(
                        f"GenX remote Web Search compatibility rejection for {call.model}"
                    )
                    continue
                remote = response.get("remote_job", {}) if isinstance(response, dict) else {}
                provider_error = str(remote.get("error") or remote.get("message") or call.error_code or "UNKNOWN") if isinstance(remote, dict) else str(call.error_code or "UNKNOWN")
                raise GenXWorkerError(f"GenX research remote job failed without safe compatibility evidence: {provider_error}")
            if call.status != "COMPLETED":
                raise GenXWorkerError(f"GenX research session ended in unexpected state: {call.status}")
            break
        except GenXError as exc:
            rejected_call = GenXCall.objects.filter(request_key=request_key).first()
            if not _confirmed_research_tool_rejection(rejected_call):
                raise
            excluded.add(rejected_call.model)
            last_rejection = exc
            continue
    else:
        raise GenXWorkerError(
            "GenX web_search tool negotiation exhausted provider-confirmed model rejections"
        ) from last_rejection

    if call is None:
        raise GenXWorkerError("GenX research session did not produce a call record")
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
        sources = list(dict.fromkeys(re.findall(r"https://[^\s)\]>]+", text)))
    return text, sources, call


def parameter_compatible_model_ids(
    model_ids: list[str] | tuple[str, ...],
    canonical_params: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> list[str]:
    """Filter live catalogue candidates by their published parameter contract."""
    rows = GenXModelCatalog.objects.filter(active=True, model_id__in=model_ids)
    compatible = [
        row.model_id
        for row in rows
        if build_model_params(row.model_payload, canonical_params, required=required) is not None
    ]
    return sorted(compatible)


def transcribe_media(request, source: Path) -> tuple[str, Any]:
    gateway = GenXGateway()
    eligible = capability_model_ids("transcription", "transcribe", "speech to text")
    estimated, call_limit = credit_envelope(request.job_id, request.inputs)
    job = Job.objects.get(pk=request.job_id)
    required_quality = Decimal(str(request.inputs.get("minimum_quality", "0.85")))
    allow_exploration = bool(request.inputs.get("allow_model_exploration", False))
    economically_fragile = bool(request.inputs.get("economically_fragile", False))
    selected = gateway.select_model(
        task_class="transcription",
        category="audio",
        eligible_model_ids=eligible,
        required_quality=required_quality,
        expected_revenue=job.reward,
        max_genx_credits=call_limit,
        estimated_credits=estimated,
        accounting_currency=job.currency,
        allow_exploration=allow_exploration,
        economically_fragile=economically_fragile,
    )
    uploaded = gateway.client.upload_file(source)
    file_id = str(uploaded.get("file_id") or uploaded.get("id") or "")
    file_url = str(uploaded.get("url") or uploaded.get("download_url") or "")
    names = model_parameter_names(selected.model_payload)
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
    call = gateway.run(
        job_id=request.job_id,
        worker_id=request.worker_id,
        category=selected.category,
        task_class="transcription",
        params=params,
        estimated_credits=estimated,
        max_allowed_credits=call_limit,
        request_key=_request_key(request, "transcription", file_id or source.name),
        eligible_model_ids=[selected.model_id],
        wait_timeout_seconds=int(os.getenv("GENX_TRANSCRIPTION_TIMEOUT_SECONDS", "600")),
        required_quality=required_quality,
        expected_revenue=job.reward,
        allow_exploration=allow_exploration,
        economically_fragile=economically_fragile,
    )
    text = _terminal_text(gateway, call)
    if file_id and call.status == "COMPLETED":
        try:
            gateway.client.delete_file(file_id)
        except Exception:
            # Cleanup failure must not invalidate a completed paid transcription.
            pass
    return text, call