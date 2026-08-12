from __future__ import annotations

import os
import re

import redis
from rq import Queue, Retry


QUEUE_NAMES = {
    "p0": "p0_revenue_protection",
    "p1": "p1_instant_claim",
    "p2": "p2_auto_accept",
    "p3": "p3_assigned",
    "p4": "p4_high_ev",
    "p5": "p5_microjobs",
    "p6": "p6_bounties",
    "p7": "p7_background",
}

_RQ_JOB_ID_PART = re.compile(r"^[A-Za-z0-9_-]+$")
_SENSITIVE_MESSAGE = re.compile(
    r"(?i)(bearer\s+)[^\s]+|((?:password|passwd|token|secret|api[_-]?key|authorization)\s*[:=]\s*)[^\s,;]+|redis://[^@\s]+@"
)


def rq_job_id(*parts: object) -> str:
    """Build one deterministic, RQ 2.10-safe ID from bounded internal identifiers."""
    if not parts:
        raise ValueError("at least one RQ job ID part is required")
    values = []
    for raw in parts:
        value = str(raw)
        if not value or len(value) > 80 or not _RQ_JOB_ID_PART.fullmatch(value):
            raise ValueError("RQ job ID parts may contain only ASCII letters, numbers, underscores and dashes")
        values.append(value)
    result = "-".join(values)
    if len(result) > 255:
        raise ValueError("RQ job ID exceeds the bounded maximum length")
    return result


def rq_failure_metadata(exc: Exception, *, queue_name: str, job_id: str) -> dict[str, str]:
    """Return bounded diagnostics without leaking credentials or connection secrets."""
    message = " ".join(str(exc).split())
    message = _SENSITIVE_MESSAGE.sub(lambda match: (match.group(1) or match.group(2) or "redis://") + "[REDACTED]", message)
    for name, value in os.environ.items():
        if any(token in name.upper() for token in ("PASSWORD", "TOKEN", "SECRET", "API_KEY")) and len(value) >= 4:
            message = message.replace(value, "[REDACTED]")
    return {
        "error_code": exc.__class__.__name__,
        "exception_class": exc.__class__.__name__,
        "error_message": message[:240],
        "queue_name": str(queue_name)[:120],
        "rq_job_id": str(job_id)[:255],
    }


def connection():
    return redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))


def queue(priority: str = "p7") -> Queue:
    return Queue(QUEUE_NAMES[priority], connection=connection())


def enqueue_agentgigs_webhook(event_id: int, event_type: str):
    from control.tasks import process_agentgigs_webhook_task

    priority = "p0" if event_type in {"job.revision_requested", "job.approved", "payment.released"} else "p7"
    return queue(priority).enqueue(
        process_agentgigs_webhook_task,
        event_id,
        job_id=rq_job_id("agentgigs", "webhook", event_id),
        retry=Retry(max=3, interval=[10, 30, 120]),
        result_ttl=86400,
        failure_ttl=604800,
    )
