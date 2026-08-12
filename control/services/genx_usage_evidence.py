from __future__ import annotations

from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal
from typing import Any

from django.db.models import Q
from django.utils.dateparse import parse_datetime

from control.models import AuditEvent, GenXAccountSnapshot, GenXCall
from gateways.genx.client import GenXError
from gateways.genx.contracts import usage_credits
from gateways.genx.service import GenXGateway


ZERO = Decimal("0")


class GenXUsageEvidenceError(RuntimeError):
    pass


def _remote_job_id(call: GenXCall, expected_remote_job_id: str = "") -> str:
    metadata = call.requested_metadata or {}
    stored = str(metadata.get("remote_job_id") or "")
    if not stored and call.external_job_id and not call.external_job_id.startswith("session:"):
        stored = call.external_job_id
    expected = str(expected_remote_job_id or "").strip()
    if expected and stored and expected != stored:
        raise GenXUsageEvidenceError(
            f"remote job identity mismatch: stored={stored} expected={expected}"
        )
    remote_job_id = stored or expected
    if not remote_job_id:
        raise GenXUsageEvidenceError("GenX call has no remote job identity for usage evidence")
    return remote_job_id


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = parse_datetime(value.strip())
    else:
        parsed = None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime_timezone.utc)
    return parsed


def _usage_from_payload(payload: Any) -> Decimal | None:
    value = usage_credits(payload)
    if value is not None:
        return value
    if isinstance(payload, dict):
        for key in ("data", "job", "result"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                value = usage_credits(nested)
                if value is not None:
                    return value
        messages = payload.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict):
                    value = usage_credits(message)
                    if value is not None:
                        return value
    return None


def _read_only_remote_evidence(
    *, gateway: GenXGateway, call: GenXCall, remote_job_id: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        job_payload = gateway.client.job(remote_job_id)
    except GenXError as exc:
        raise GenXUsageEvidenceError("existing remote GenX job could not be retrieved") from exc

    result_payload: dict[str, Any] = {}
    try:
        result_payload = gateway.client.result(remote_job_id)
    except GenXError:
        result_payload = {}

    history_payload: dict[str, Any] = {}
    session_id = str((call.requested_metadata or {}).get("session_id") or "")
    if session_id:
        try:
            history_payload = gateway.client.session_messages(session_id)
        except GenXError:
            history_payload = {}

    return job_payload, result_payload, history_payload


def _record_evidence(
    *, call: GenXCall, method: str, evidence: dict[str, Any]
) -> GenXCall:
    metadata = dict(call.requested_metadata or {})
    metadata["billing_evidence_method"] = method
    metadata["billing_evidence"] = evidence
    GenXCall.objects.filter(pk=call.pk).update(requested_metadata=metadata)
    call.requested_metadata = metadata
    if not AuditEvent.objects.filter(
        event_type="genx.billing_usage_evidence_resolved",
        metadata__call_id=str(call.id),
    ).exists():
        AuditEvent.objects.create(
            event_type="genx.billing_usage_evidence_resolved",
            actor="genx-usage-evidence",
            metadata={
                "call_id": str(call.id),
                "remote_job_id": str(evidence.get("remote_job_id") or ""),
                "method": method,
                "credits": str(call.credits),
                "billing_truth": str(metadata.get("billing_truth") or ""),
                "evidence": evidence,
            },
        )
    return call


def _direct_remote_usage(
    *,
    gateway: GenXGateway,
    call: GenXCall,
    remote_job_id: str,
    job_payload: dict[str, Any],
    result_payload: dict[str, Any],
    history_payload: dict[str, Any],
) -> GenXCall | None:
    sources = (
        ("REMOTE_JOB", job_payload),
        ("REMOTE_RESULT", result_payload),
        ("SESSION_HISTORY", history_payload),
    )
    for source_name, payload in sources:
        value = _usage_from_payload(payload)
        if value is None or value < ZERO:
            continue
        enriched = dict(job_payload)
        enriched["usage"] = {"credits": str(value), "evidence_source": source_name}
        reconciled = gateway.reconcile_remote_job_payload(
            call.id,
            enriched,
            source="OPERATOR_EVIDENCE",
        )
        reconciled.refresh_from_db()
        return _record_evidence(
            call=reconciled,
            method="DIRECT_REMOTE_USAGE",
            evidence={
                "remote_job_id": remote_job_id,
                "source": source_name,
                "credits": str(value),
            },
        )
    return None


def _possible_competing_calls(
    *, call: GenXCall, before: GenXAccountSnapshot, after: GenXAccountSnapshot
) -> list[GenXCall]:
    window = (
        Q(created_at__gte=before.created_at, created_at__lte=after.created_at)
        | Q(started_at__gte=before.created_at, started_at__lte=after.created_at)
        | Q(completed_at__gte=before.created_at, completed_at__lte=after.created_at)
        | (
            Q(started_at__lt=before.created_at)
            & (Q(completed_at__isnull=True) | Q(completed_at__gte=before.created_at))
        )
    )
    possible = []
    for other in GenXCall.objects.exclude(pk=call.pk).filter(window).order_by("created_at"):
        metadata = other.requested_metadata or {}
        billing_truth = str(metadata.get("billing_truth") or "")
        if billing_truth == "NOT_APPLICABLE":
            continue
        if billing_truth == "ACTUAL" and other.credits == ZERO:
            continue
        possible.append(other)
    return possible


def _unique_account_delta_usage(
    *,
    gateway: GenXGateway,
    call: GenXCall,
    remote_job_id: str,
    job_payload: dict[str, Any],
) -> GenXCall:
    remote_start = _as_datetime(job_payload.get("created_at"))
    remote_end = _as_datetime(job_payload.get("updated_at"))
    start_candidates = [value for value in (remote_start, call.started_at, call.created_at) if value is not None]
    if not start_candidates:
        raise GenXUsageEvidenceError("cannot establish the completed GenX call start time")
    event_start = min(start_candidates)
    event_end = remote_end or remote_start or event_start
    if event_end < event_start:
        raise GenXUsageEvidenceError("remote GenX timestamps are inconsistent")

    before = (
        GenXAccountSnapshot.objects.filter(created_at__lte=event_start)
        .exclude(available_credits__isnull=True)
        .order_by("-created_at")
        .first()
    )
    after = (
        GenXAccountSnapshot.objects.filter(created_at__gte=event_end)
        .exclude(available_credits__isnull=True)
        .order_by("created_at")
        .first()
    )
    if before is None or after is None or before.pk == after.pk:
        raise GenXUsageEvidenceError(
            "completed GenX call is not bracketed by two account credit snapshots"
        )

    before_credits = Decimal(before.available_credits)
    after_credits = Decimal(after.available_credits)
    delta = before_credits - after_credits
    if delta <= ZERO:
        raise GenXUsageEvidenceError(
            "bracketing GenX account snapshots do not prove a positive credit charge"
        )
    if call.max_allowed_credits <= ZERO or delta > call.max_allowed_credits:
        raise GenXUsageEvidenceError(
            "account snapshot delta exceeds the call's controller credit ceiling"
        )

    competing = _possible_competing_calls(call=call, before=before, after=after)
    if competing:
        raise GenXUsageEvidenceError(
            "account snapshot delta is ambiguous because another potentially billable GenX call overlaps the evidence window"
        )

    evidence = {
        "remote_job_id": remote_job_id,
        "source": "GENX_ACCOUNT_CREDIT_LEDGER",
        "attribution": "UNIQUE_CALL_IN_BRACKETED_SNAPSHOT_WINDOW",
        "remote_created_at": remote_start.isoformat() if remote_start else None,
        "remote_updated_at": remote_end.isoformat() if remote_end else None,
        "before_snapshot_id": before.pk,
        "before_at": before.created_at.isoformat(),
        "before_available_credits": str(before_credits),
        "after_snapshot_id": after.pk,
        "after_at": after.created_at.isoformat(),
        "after_available_credits": str(after_credits),
        "derived_credits": str(delta),
        "competing_billable_calls": 0,
        "call_credit_ceiling": str(call.max_allowed_credits),
    }
    enriched = dict(job_payload)
    enriched["usage"] = {
        "credits": str(delta),
        "evidence_source": "GENX_ACCOUNT_SNAPSHOT_DELTA",
    }
    reconciled = gateway.reconcile_remote_job_payload(
        call.id,
        enriched,
        source="OPERATOR_EVIDENCE",
    )
    reconciled.refresh_from_db()
    if str((reconciled.requested_metadata or {}).get("billing_truth") or "") != "ACTUAL":
        raise GenXUsageEvidenceError("account snapshot evidence did not resolve actual billing truth")
    return _record_evidence(
        call=reconciled,
        method="ACCOUNT_SNAPSHOT_DELTA_UNIQUE_ATTRIBUTION",
        evidence=evidence,
    )


def resolve_missing_genx_usage(
    call_id,
    *,
    expected_remote_job_id: str = "",
    gateway: GenXGateway | None = None,
) -> GenXCall:
    """Resolve missing usage without replaying any provider mutation.

    Direct provider usage wins. If historical read endpoints no longer expose usage,
    an account credit delta is accepted only when two persisted snapshots bracket the
    provider job and no other potentially billable GenX call overlaps that window.
    """
    gateway = gateway or GenXGateway()
    call = GenXCall.objects.get(pk=call_id)
    if str((call.requested_metadata or {}).get("billing_truth") or "") == "ACTUAL":
        return call

    remote_job_id = _remote_job_id(call, expected_remote_job_id)
    job_payload, result_payload, history_payload = _read_only_remote_evidence(
        gateway=gateway,
        call=call,
        remote_job_id=remote_job_id,
    )
    status = str(job_payload.get("status") or "").upper()
    if status != "COMPLETED":
        return call

    direct = _direct_remote_usage(
        gateway=gateway,
        call=call,
        remote_job_id=remote_job_id,
        job_payload=job_payload,
        result_payload=result_payload,
        history_payload=history_payload,
    )
    if direct is not None:
        return direct

    return _unique_account_delta_usage(
        gateway=gateway,
        call=call,
        remote_job_id=remote_job_id,
        job_payload=job_payload,
    )
