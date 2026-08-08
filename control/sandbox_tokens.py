from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


class SandboxTokenError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxTokenClaims:
    job_id: str
    worker_id: str
    model: str
    max_credits: Decimal
    expires_at: int
    nonce: str


def _secret() -> bytes:
    value = os.getenv("SANDBOX_TOKEN_SECRET", "").encode()
    if len(value) < 32:
        raise SandboxTokenError("SANDBOX_TOKEN_SECRET must be at least 32 bytes")
    return value


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_sandbox_token(*, job_id: str, worker_id: str, model: str, max_credits: Decimal, ttl_seconds: int = 1800) -> str:
    ttl = max(60, min(int(ttl_seconds), 7200))
    payload = {
        "v": 1,
        "job": str(job_id),
        "worker": str(worker_id),
        "model": str(model),
        "max_credits": str(Decimal(max_credits)),
        "exp": int(time.time()) + ttl,
        "nonce": secrets.token_hex(16),
    }
    encoded = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64(hmac.new(_secret(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_sandbox_token(token: str, *, now: int | None = None) -> SandboxTokenClaims:
    try:
        encoded, signature = token.split(".", 1)
        expected = _b64(hmac.new(_secret(), encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise SandboxTokenError("sandbox token signature is invalid")
        payload: dict[str, Any] = json.loads(_unb64(encoded))
        if payload.get("v") != 1:
            raise SandboxTokenError("sandbox token version is unsupported")
        expires_at = int(payload["exp"])
        if expires_at <= int(time.time() if now is None else now):
            raise SandboxTokenError("sandbox token has expired")
        credits = Decimal(str(payload["max_credits"]))
        if credits <= 0:
            raise SandboxTokenError("sandbox token credit ceiling must be positive")
        job_id = str(payload["job"])
        worker_id = str(payload["worker"])
        model = str(payload["model"])
        nonce = str(payload["nonce"])
        if not all((job_id, worker_id, model, nonce)):
            raise SandboxTokenError("sandbox token is incomplete")
        return SandboxTokenClaims(job_id, worker_id, model, credits, expires_at, nonce)
    except SandboxTokenError:
        raise
    except Exception as exc:
        raise SandboxTokenError("sandbox token could not be decoded") from exc
