from __future__ import annotations

from decimal import Decimal

from control.models import Application, AuditEvent, Bid, Claim, Job
from control.services.jobs import acquisition_decision, transition_job
from control.services.locks import JobLockUnavailable, acquire_job_lock, release_job_lock
from markets.base import MarketAdapter


class AcquisitionError(RuntimeError):
    pass


def _normalized(adapter: MarketAdapter, job: Job):
    return adapter.normalize_job(job.normalized_payload)


def _ensure_no_active_attempt(job: Job, action: str) -> None:
    if action == "CLAIM" and Claim.objects.filter(job=job, status__in=["SUBMITTING", "CLAIMED"]).exists():
        raise AcquisitionError("claim already exists or may have been submitted remotely")
    if action == "BID" and Bid.objects.filter(job=job, status__in=["SUBMITTING", "SUBMITTED", "ACCEPTED"]).exists():
        raise AcquisitionError("bid already exists or may have been submitted remotely")
    if action == "APPLY" and Application.objects.filter(job=job).exists():
        raise AcquisitionError("application already exists for this job; duplicate applications are not permitted")


def acquire_profitable_job(
    *,
    adapter: MarketAdapter,
    job_id,
    node_id: str,
    action: str,
    offered_price: Decimal | None = None,
    message: str = "",
) -> dict:
    """Execute one approved acquisition action under the global lease and persist remote truth."""
    job = Job.objects.select_related("marketplace", "jobscore").get(pk=job_id)
    if adapter.slug != job.marketplace.slug:
        raise AcquisitionError("adapter does not match job marketplace")
    gate = acquisition_decision(job)
    if not gate.allowed:
        raise AcquisitionError("acquisition blocked: " + ",".join(gate.reason_codes))
    if job.state != Job.State.EXPECTED:
        raise AcquisitionError(f"job must be EXPECTED before acquisition, got {job.state}")

    action = action.upper()
    lock = acquire_job_lock(job.id, node_id=node_id, lease_seconds=180)
    attempt = None
    try:
        _ensure_no_active_attempt(job, action)
        opportunity = _normalized(adapter, job)
        if action == "CLAIM":
            if not adapter.capabilities.claim:
                raise AcquisitionError("market adapter does not support claim")
            attempt = Claim.objects.create(job=job, status="SUBMITTING")
            response = adapter.claim(opportunity)
            remote_reference = str(response.get("id") or response.get("contract_id") or response.get("claim_id") or "") if isinstance(response, dict) else ""
            attempt.remote_reference = remote_reference
            attempt.status = "CLAIMED"
            attempt.save(update_fields=["remote_reference", "status", "updated_at"])
            transition_job(job.id, Job.State.CLAIMED, actor="acquisition", metadata={"action": action, "remote_reference": remote_reference})
        elif action == "BID":
            if not adapter.capabilities.bid:
                raise AcquisitionError("market adapter does not support bid")
            if offered_price is None or offered_price <= 0:
                raise AcquisitionError("positive offered_price is required for bid")
            attempt = Bid.objects.create(job=job, amount=offered_price, currency=job.currency, status="SUBMITTING")
            response = adapter.bid(opportunity, offered_price)
            remote_reference = str(response.get("id") or response.get("bid_id") or "") if isinstance(response, dict) else ""
            attempt.remote_reference = remote_reference
            attempt.status = "SUBMITTED"
            attempt.save(update_fields=["remote_reference", "status", "updated_at"])
        elif action == "APPLY":
            if not adapter.capabilities.apply:
                raise AcquisitionError("market adapter does not support apply")
            if offered_price is None or offered_price <= 0:
                raise AcquisitionError("positive offered_price is required for application")
            attempt = Application.objects.create(
                job=job,
                action="APPLY",
                offered_price=offered_price,
                currency=job.currency,
                message=message,
                status="SUBMITTING",
            )
            response = adapter.apply(opportunity, offered_price, message)
            remote_reference = str(response.get("id") or response.get("application_id") or response.get("applicationId") or response.get("contract_id") or "") if isinstance(response, dict) else ""
            attempt.remote_reference = remote_reference
            attempt.status = "SUBMITTED"
            attempt.save(update_fields=["remote_reference", "status", "updated_at"])
        else:
            raise AcquisitionError(f"unsupported acquisition action: {action}")

        AuditEvent.objects.create(
            event_type="job.acquisition_submitted",
            actor="acquisition",
            metadata={"job_id": str(job.id), "market": job.marketplace.slug, "action": action},
        )
        return response if isinstance(response, dict) else {"result": response}
    except Exception as exc:
        if attempt is not None and getattr(attempt, "status", "") == "SUBMITTING":
            attempt.status = "UNKNOWN_REMOTE_STATE"
            attempt.save(update_fields=["status", "updated_at"])
            AuditEvent.objects.create(
                severity="ERROR",
                event_type="job.acquisition_unknown_remote_state",
                actor="acquisition",
                metadata={"job_id": str(job.id), "action": action, "error_code": exc.__class__.__name__},
            )
        raise
    finally:
        try:
            release_job_lock(job.id, node_id=node_id, fencing_token=lock.fencing_token)
        except JobLockUnavailable:
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="job.lock_release_stale",
                actor="acquisition",
                metadata={"job_id": str(job.id), "node": node_id},
            )
