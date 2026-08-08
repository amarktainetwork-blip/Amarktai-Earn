from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any


class AgentGigsWebhookError(ValueError):
    pass


SUPPORTED_EVENTS = {
    "job.available",
    "job.accepted",
    "job.revision_requested",
    "job.approved",
    "payment.released",
}


@dataclass(frozen=True)
class AgentGigsWebhook:
    event: str
    timestamp: str
    data: dict[str, Any]

    @property
    def external_job_id(self) -> str:
        job = self.data.get("job")
        if isinstance(job, dict) and job.get("id"):
            return str(job["id"])
        for key in ("job_id", "jobId"):
            if self.data.get(key):
                return str(self.data[key])
        return ""


def signature_for(raw_body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    supplied = signature.strip()
    if supplied.lower().startswith("sha256="):
        supplied = supplied.split("=", 1)[1]
    return hmac.compare_digest(signature_for(raw_body, secret), supplied.lower())


def event_key(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def parse_webhook(raw_body: bytes) -> AgentGigsWebhook:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentGigsWebhookError("invalid webhook JSON") from exc
    if not isinstance(payload, dict):
        raise AgentGigsWebhookError("webhook payload must be an object")
    event = str(payload.get("event") or "")
    if event not in SUPPORTED_EVENTS:
        raise AgentGigsWebhookError(f"unsupported webhook event: {event}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AgentGigsWebhookError("webhook data must be an object")
    return AgentGigsWebhook(event=event, timestamp=str(payload.get("timestamp") or ""), data=data)
