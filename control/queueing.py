from __future__ import annotations

import os

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
        job_id=f"agentgigs:webhook:{event_id}",
        retry=Retry(max=3, interval=[10, 30, 120]),
        result_ttl=86400,
        failure_ttl=604800,
    )
