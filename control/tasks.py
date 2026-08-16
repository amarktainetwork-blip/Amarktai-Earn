from __future__ import annotations

from control.models import Job
from control.services.agentgigs import (
    process_pending_webhooks,
    process_webhook_event,
    sync_applications,
    sync_job,
    sync_market,
)


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


def execute_work_plan_task(plan_id: int):
    from planning.services import execute_work_plan

    plan = execute_work_plan(plan_id)
    return {"plan_id": plan.id, "status": plan.status, "execution_attempts": plan.execution_attempts, "repair_attempts": plan.repair_attempts}


def submit_work_plan_task(plan_id: int):
    from planning.services import submit_work_plan

    plan = submit_work_plan(plan_id)
    return {"plan_id": plan.id, "status": plan.status}


def execute_commercial_api_request_task(request_id: str):
    from control.models import CommercialAPIRequest
    from control.services.commercial_api import execute_request

    row = execute_request(CommercialAPIRequest.objects.select_related("product", "api_key__plan").get(pk=request_id))
    return {"request_id": str(row.id), "status": row.status, "qa_passed": row.qa_passed}


def autonomous_income_frequent_task():
    from control.services.autonomous_income import run_autonomous_frequent_cycle
    from control.services.autonomy import AutonomyMode, current_mode
    from control.services.markets import configured_adapter

    adapters = {}
    if current_mode() in {AutonomyMode.LOW_RISK, AutonomyMode.FULL}:
        for slug in ("taskbounty", "opire", "algora", "gitpay"):
            try:
                adapters[slug] = configured_adapter(slug)
            except (KeyError, ValueError):
                continue
    return {**run_autonomous_frequent_cycle(adapters=adapters), "hook": "FREQUENT_DISCOVERY_AND_RECONCILIATION"}


def autonomous_income_bounded_work_task():
    from control.services.autonomous_income import run_autonomous_earn_loop
    from control.services.autonomy import AutonomyMode, current_mode
    from control.services.markets import configured_adapter

    adapters = {}
    if current_mode() in {AutonomyMode.LOW_RISK, AutonomyMode.FULL}:
        for slug in ("taskbounty", "opire", "algora", "gitpay"):
            try:
                adapters[slug] = configured_adapter(slug)
            except (KeyError, ValueError):
                continue
    return {**run_autonomous_earn_loop(adapters=adapters), "hook": "BOUNDED_RANK_AND_WORK"}


def autonomous_income_daily_task():
    from control.services.autonomous_income import run_autonomous_daily_cycle
    from control.services.autonomy import AutonomyMode, current_mode
    from control.services.markets import configured_adapter

    adapters = {}
    if current_mode() in {AutonomyMode.LOW_RISK, AutonomyMode.FULL}:
        for slug in ("taskbounty", "opire", "algora", "gitpay"):
            try:
                adapters[slug] = configured_adapter(slug)
            except (KeyError, ValueError):
                continue
    return {**run_autonomous_daily_cycle(adapters=adapters), "hook": "DAILY_SETTLEMENT_AND_PERFORMANCE"}
