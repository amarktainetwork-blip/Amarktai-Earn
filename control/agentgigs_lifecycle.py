from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class RemoteLifecycleDecision:
    awarded: bool = False
    submitted: bool = False
    revision_required: bool = False
    approved: bool = False
    payment_released: bool = False


def cents(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = int(Decimal(str(value)))
    except Exception:
        return None
    return amount if amount >= 0 else None


def application_is_awarded(status: str | None) -> bool:
    # AgentGigs distinguishes accepted selection from funded escrow. Amarktai only
    # treats funded work as awarded/protected work.
    return str(status or "").lower() == "funded"


def details_decision(payload: dict[str, Any]) -> RemoteLifecycleDecision:
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    application = payload.get("myApplication") if isinstance(payload.get("myApplication"), dict) else {}
    remote_status = str(job.get("status") or "").lower()
    assigned = bool(payload.get("isAssigned"))
    awarded = assigned and (application_is_awarded(application.get("status")) or remote_status == "in_progress")
    submitted = assigned and remote_status in {"delivered", "pending_proof"}
    return RemoteLifecycleDecision(awarded=awarded, submitted=submitted)


def webhook_decision(event: str) -> RemoteLifecycleDecision:
    if event == "job.revision_requested":
        return RemoteLifecycleDecision(revision_required=True)
    if event == "job.approved":
        return RemoteLifecycleDecision(approved=True)
    if event == "payment.released":
        return RemoteLifecycleDecision(payment_released=True)
    return RemoteLifecycleDecision()


def authoritative_payout_from_earnings(payload: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal] | None:
    calculator = payload.get("calculator")
    if not isinstance(calculator, dict):
        return None
    job_amount = cents(calculator.get("jobAmount"))
    commission = cents(calculator.get("commissionAmount"))
    payout = cents(calculator.get("agentPayout"))
    if job_amount is None or commission is None or payout is None:
        return None
    if job_amount - commission != payout:
        return None
    hundred = Decimal("100")
    return Decimal(job_amount) / hundred, Decimal(commission) / hundred, Decimal(payout) / hundred
