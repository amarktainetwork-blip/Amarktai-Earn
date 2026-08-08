from __future__ import annotations

import hashlib
import json
import os
import time
from decimal import Decimal
from typing import Any

import redis
import requests
from django.utils import timezone

from control.models import AuditEvent, GenXCall
from control.sandbox_tokens import SandboxTokenClaims, verify_sandbox_token
from gateways.genx.service import GenXGateway


class SandboxGenXProxyError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _canonical_request(claims: SandboxTokenClaims, body: dict[str, Any]) -> str:
    material = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{claims.nonce}|{material}".encode()).hexdigest()
    return f"sandbox-proxy:{digest[:48]}"


def _redis_client():
    return redis.Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)


def _cache_key(request_key: str) -> str:
    return f"amarktai:sandbox:llm-response:{request_key}"


def _cached_response(cache, request_key: str) -> dict[str, Any] | None:
    try:
        raw = cache.get(_cache_key(request_key))
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _store_response(cache, request_key: str, payload: dict[str, Any]) -> None:
    try:
        cache.setex(_cache_key(request_key), 86400, json.dumps(payload, separators=(",", ":"), default=str))
    except Exception:
        pass


def _normalize_model(value: Any) -> str:
    model = str(value or "")
    if model.startswith("openai/"):
        model = model.split("/", 1)[1]
    return model


def proxy_chat_completion(token: str, body: dict[str, Any], *, session=None, cache=None) -> tuple[dict[str, Any], bool]:
    claims = verify_sandbox_token(token)
    if not isinstance(body, dict):
        raise SandboxGenXProxyError("request body must be a JSON object")
    requested_model = _normalize_model(body.get("model"))
    if requested_model != claims.model:
        raise SandboxGenXProxyError("sandbox token is not authorized for requested model", status_code=403)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise SandboxGenXProxyError("messages must be a non-empty list")

    clean = dict(body)
    requested_stream = bool(clean.pop("stream", False))
    clean.pop("stream_options", None)
    clean["model"] = claims.model
    clean["stream"] = False

    request_key = _canonical_request(claims, clean)
    cache = cache or _redis_client()
    gateway = GenXGateway()
    estimated = Decimal(os.getenv("SANDBOX_LLM_ESTIMATED_CREDITS", "0.25"))
    if estimated <= 0:
        estimated = Decimal("0.25")
    estimated = min(estimated, claims.max_credits)
    call, created = gateway._reserve_call(
        job_id=claims.job_id,
        worker_id=claims.worker_id,
        model=claims.model,
        task_class="coding_sandbox",
        estimated_credits=estimated,
        max_allowed_credits=claims.max_credits,
        request_key=request_key,
        metadata={"transport": "sandbox-openai-proxy", "sandbox_nonce": claims.nonce, "model_requested": claims.model},
    )
    if not created:
        cached = _cached_response(cache, request_key)
        if call.status == "COMPLETED" and cached is not None:
            return cached, requested_stream
        raise SandboxGenXProxyError("matching LLM request already exists; automatic replay is blocked", status_code=409)

    upstream = session or requests.Session()
    url = os.getenv("GENX_BASE_URL", "https://query.genx.sh").rstrip("/") + "/v1/chat/completions"
    master_key = os.getenv("GENX_API_KEY", "").strip()
    if not master_key:
        GenXCall.objects.filter(pk=call.pk).update(status="FAILED", completed_at=timezone.now(), error_code="GENX_API_KEY_MISSING")
        raise SandboxGenXProxyError("GenX is not configured", status_code=503)
    started = time.monotonic()
    try:
        response = upstream.post(
            url,
            headers={"Authorization": f"Bearer {master_key}", "Content-Type": "application/json", "User-Agent": "amarktai-earn-sandbox-proxy/1"},
            json=clean,
            timeout=max(15, min(int(os.getenv("SANDBOX_LLM_TIMEOUT_SECONDS", "180")), 600)),
        )
    except requests.RequestException as exc:
        GenXCall.objects.filter(pk=call.pk).update(
            status="UNKNOWN_REMOTE_STATE",
            latency_ms=int((time.monotonic() - started) * 1000),
            error_code=exc.__class__.__name__[:120],
        )
        AuditEvent.objects.create(
            severity="WARNING",
            event_type="genx.sandbox_proxy_unknown_remote_state",
            actor="sandbox-genx-proxy",
            metadata={"call_id": str(call.id), "job_id": claims.job_id, "error_code": exc.__class__.__name__},
        )
        raise SandboxGenXProxyError("GenX proxy request entered unknown remote state", status_code=502) from exc

    if not response.ok:
        definite = 400 <= response.status_code < 500 and response.status_code != 429
        GenXCall.objects.filter(pk=call.pk).update(
            status="FAILED" if definite else "UNKNOWN_REMOTE_STATE",
            latency_ms=int((time.monotonic() - started) * 1000),
            completed_at=timezone.now() if definite else None,
            error_code=f"HTTP_{response.status_code}",
        )
        raise SandboxGenXProxyError(f"GenX upstream returned {response.status_code}", status_code=502)
    try:
        payload = response.json()
    except ValueError as exc:
        GenXCall.objects.filter(pk=call.pk).update(status="UNKNOWN_REMOTE_STATE", error_code="INVALID_JSON")
        raise SandboxGenXProxyError("GenX upstream returned invalid JSON", status_code=502) from exc
    if not isinstance(payload, dict):
        raise SandboxGenXProxyError("GenX upstream returned invalid response", status_code=502)

    gateway.reconcile(
        call.id,
        {
            "status": "COMPLETED",
            "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
            "billing": payload.get("billing") if isinstance(payload.get("billing"), dict) else {},
        },
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
    _store_response(cache, request_key, payload)
    return payload, requested_stream


def stream_wrapper(payload: dict[str, Any]) -> bytes:
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
    finish = "stop"
    delta: dict[str, Any] = {"role": "assistant"}
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if content not in (None, ""):
                delta["content"] = str(content)
            if isinstance(message.get("tool_calls"), list):
                delta["tool_calls"] = message["tool_calls"]
            if isinstance(message.get("function_call"), dict):
                delta["function_call"] = message["function_call"]
        finish = str(choices[0].get("finish_reason") or "stop")
    chunk = {
        "id": payload.get("id") or "amarktai-sandbox-proxy",
        "object": "chat.completion.chunk",
        "created": payload.get("created") or int(time.time()),
        "model": payload.get("model") or "",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if isinstance(payload.get("usage"), dict):
        chunk["usage"] = payload["usage"]
    return ("data: " + json.dumps(chunk, separators=(",", ":")) + "\n\ndata: [DONE]\n\n").encode()
