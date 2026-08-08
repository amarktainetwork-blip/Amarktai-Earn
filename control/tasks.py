from __future__ import annotations

from control.models import Job
from control.services.agentgigs import process_pending_webhooks, process_webhook_event, sync_applications, sync_job, sync_market


def sync_agentgigs_market_task():
    return sync_market()


def sync_agentgigs_applications_task():
    return sync_applications()


def process_agentgigs_webhooks_task():
    return process_pending_webhooks()


def sync_agentgigs_active_jobs_task(limit: int = 100):
    results = {"synced": 0, "failed": 0}
    jobs = Job.objects.filter(
        marketplace__slug="agentgigs",
        state__in=[Job.State.EXPECTED, Job.State.AWARDED, Job.State.EXECUTING, Job.State.SUBMITTED, Job.State.ACCEPTED, Job.State.PAYOUT_PENDING],
    ).order_by("updated_at")[:limit]
    for job in jobs:
        try:
            sync_job(job)
            results["synced"] += 1
        except Exception:
            results["failed"] += 1
    return results


def process_agentgigs_webhook_task(event_id: int):
    event = process_webhook_event(event_id)
    return {"event_id": event.id, "status": event.status}


def submit_agentgigs_job_task(job_id, notes: str = "Completed and independently QA verified."):
    from control.services.agentgigs import configured_adapter
    from control.services.submission import submit_qa_passed_job

    submission = submit_qa_passed_job(adapter=configured_adapter(), job_id=job_id, notes=notes)
    return {"submission_id": submission.id, "status": submission.status}
