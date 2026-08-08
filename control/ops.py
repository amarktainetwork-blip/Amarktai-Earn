from __future__ import annotations

import os
import shutil
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.db.models import Count, Q, Sum
from django.utils import timezone

from planning.models import DependencyPreparation, WorkPlan
from workers.registry import registry_manifest
from workers.genx_support import catalog_supports
from .models import (
    Alert,
    AdmissionDecision,
    AcquisitionPreflight,
    Application,
    Bid,
    AuthThrottle,
    Artifact,
    AuditEvent,
    Execution,
    GenXAccountSnapshot,
    GenXCall,
    Job,
    Claim,
    LedgerEntry,
    Marketplace,
    MarketplaceCredential,
    ModelStat,
    Node,
    OwnerSecurityProfile,
    Payout,
    QAResult,
    RecoveryCode,
    Revision,
    ReauthenticationGrant,
    ResourceSnapshot,
    ServiceHeartbeat,
    RefreshSession,
    SystemSetting,
    Submission,
    TreasuryBalance,
    Worker,
)
from control.services.autonomy import current_mode

SECTIONS = (
    "overview",
    "live-work",
    "agents",
    "markets",
    "earnings",
    "treasury",
    "genx",
    "nodes",
    "storage",
    "performance",
    "logs",
    "alerts",
    "settings",
    "security",
)


def _dt(value):
    return value.isoformat() if value else None


def _dec(value):
    if value is None:
        return None
    return str(value)


def _valid_runtime_secret(name: str) -> bool:
    value = os.getenv(name, "")
    lowered = value.casefold()
    return len(value.encode()) >= 32 and not any(marker in lowered for marker in ("replace", "change-me", "dev-only"))


def _disk_row(label: str, path: str) -> dict:
    candidate = Path(path)
    try:
        usage = shutil.disk_usage(candidate if candidate.exists() else candidate.parent)
        percent = 0 if usage.total <= 0 else round((usage.used / usage.total) * 100, 2)
        return {
            "name": label,
            "path": str(candidate),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": percent,
            "status": "CRITICAL" if percent >= 90 else "WARNING" if percent >= 80 else "OK",
        }
    except OSError as exc:
        return {"name": label, "path": str(candidate), "status": "UNAVAILABLE", "error": exc.__class__.__name__}


def overview_snapshot() -> dict:
    today = timezone.localdate()
    earned = Payout.objects.filter(
        state__in=[Payout.State.EARNED, Payout.State.PAYOUT_PENDING, Payout.State.SETTLED],
        earned_at__date=today,
    ).aggregate(v=Sum("net"))["v"] or Decimal("0")
    settled = Payout.objects.filter(state=Payout.State.SETTLED, settled_at__date=today).aggregate(v=Sum("net"))["v"] or Decimal("0")
    pending = Payout.objects.filter(state=Payout.State.PAYOUT_PENDING).aggregate(v=Sum("net"))["v"] or Decimal("0")
    genx_used = GenXCall.objects.filter(created_at__date=today).aggregate(v=Sum("credits"))["v"] or Decimal("0")
    latest_genx = GenXAccountSnapshot.objects.order_by("-created_at").first()
    open_alerts = Alert.objects.filter(status="OPEN").count()
    blocked_acquisitions = AcquisitionPreflight.objects.filter(allowed=False, created_at__gte=timezone.now() - timedelta(hours=24)).count()
    unknown_remote = sum(
        model.objects.filter(status="UNKNOWN_REMOTE_STATE").count()
        for model in (Application, Bid, Claim, Submission, GenXCall)
    )
    resource = ResourceSnapshot.objects.order_by("-created_at").first()
    return {
        "section": "overview",
        "cards": [
            {"label": "NET EARNED TODAY", "value": f"${earned}", "truth": "accepted/pending/settled earnings"},
            {"label": "SETTLED TODAY", "value": f"${settled}", "truth": "received cash only"},
            {"label": "PENDING PAYOUT", "value": f"${pending}", "truth": "not received cash"},
            {"label": "ACTIVE PAID JOBS", "value": Job.objects.filter(state__in=[Job.State.CLAIMED, Job.State.AWARDED, Job.State.EXECUTING]).count()},
            {"label": "ACTIVE AGENTS", "value": Worker.objects.exclude(status__in=["OFFLINE", "READY"]).count()},
            {"label": "OPEN ALERTS", "value": open_alerts},
            {"label": "BLOCKED ACQUISITIONS 24H", "value": blocked_acquisitions, "truth": "persisted fail-closed preflight decisions"},
            {"label": "UNKNOWN REMOTE STATE", "value": unknown_remote, "truth": "requires deterministic reconciliation; never blind retry"},
            {"label": "RESOURCE GOVERNOR", "value": "GREEN" if resource and resource.healthy else "BLOCKED" if resource else "NO SNAPSHOT", "truth": ", ".join(resource.blocker_codes) if resource and resource.blocker_codes else "latest persisted admission state"},
            {"label": "GENX BALANCE", "value": "—" if not latest_genx or latest_genx.available_credits is None else f"{latest_genx.available_credits} cr"},
            {"label": "GENX USED TODAY", "value": f"{genx_used} cr"},
        ],
        "meta": {
            "autonomous_mode": current_mode().value,
            "registered_workers": len(registry_manifest()),
            "revenue_truth": "Expected opportunity values are never earnings. Accepted/pending values are not cash. Only SETTLED is received cash.",
        },
    }


def live_work_snapshot(limit: int = 100) -> dict:
    jobs = Job.objects.select_related("marketplace").order_by("-updated_at")[:limit]
    rows = []
    for job in jobs:
        execution = job.executions.select_related("worker").order_by("-attempt").first()
        try:
            plan = job.work_plan
        except WorkPlan.DoesNotExist:
            plan = None
        latest_qa = QAResult.objects.filter(job=job).order_by("-created_at").first()
        submission = Submission.objects.filter(job=job).order_by("-version", "-created_at").first()
        dependency = DependencyPreparation.objects.filter(job=job).order_by("-updated_at").first()
        rows.append({
            "job": str(job.id),
            "market": job.marketplace.slug,
            "title": job.title,
            "task_class": job.task_class,
            "state": job.state,
            "reward": f"{job.currency} {job.reward}",
            "plan": plan.status if plan else "—",
            "worker": execution.worker.worker_class if execution and execution.worker else "—",
            "operation": plan.operation if plan else "",
            "plan_blockers": plan.reason_codes if plan else [],
            "worker_id": execution.worker_id if execution else None,
            "execution": execution.status if execution else "—",
            "attempt": execution.attempt if execution else None,
            "qa": "PASS" if latest_qa and latest_qa.passed else "FAIL" if latest_qa else "—",
            "repair_attempts": f"{plan.repair_attempts}/{plan.max_repair_attempts}" if plan else None,
            "last_error": plan.last_error_code if plan else "",
            "submission": submission.status if submission else None,
            "submission_version": submission.version if submission else None,
            "open_revisions": Revision.objects.filter(job=job, status="REQUIRED").count(),
            "dependency_preparation": dependency.status if dependency else None,
            "artifacts": Artifact.objects.filter(job=job).count(),
            "deadline": _dt(job.deadline),
            "updated": _dt(job.updated_at),
        })
    return {"section": "live-work", "rows": rows}


def agents_snapshot() -> dict:
    runtime = {row.id: row for row in Worker.objects.select_related("current_job").order_by("worker_class", "id")}
    manifest = registry_manifest()
    genx_requirements = {
        "documents": ((), "text"),
        "research": ((), "text"),
        "localization": (("translation", "translate", "localization"), "text"),
        "transcription": (("transcription", "transcribe", "speech to text"), None),
        "code_small": (("code", "coding", "software"), "text"),
        "code_heavy": (("code", "coding", "software"), "text"),
        "technical_documentation": ((), "text"),
        "content_copy": ((), "text"),
        "customer_support": ((), "text"),
    }
    rows = []
    for spec in manifest:
        reasons = []
        disabled = {item.strip() for item in os.getenv("WORKER_DISABLED_CLASSES", "").split(",") if item.strip()}
        if spec["worker_class"] in disabled:
            reasons.append("WORKER_DISABLED")
        if not spec["runtime_available"]:
            reasons.append("RUNTIME_UNAVAILABLE")
        if spec["worker_class"] == "public_web_data" and os.getenv("PUBLIC_WEB_DATA_ENABLED", "0") != "1":
            reasons.append("PUBLIC_WEB_DATA_DISABLED")
        if spec["worker_class"] == "ai_safety_research" and os.getenv("SAFETY_BOUNTY_EXECUTION_ENABLED", "0") != "1":
            reasons.append("SAFETY_BOUNTY_EXECUTION_DISABLED")
        sandbox_worker = spec["worker_class"] in {"code_small", "code_heavy", "ci_testing"}
        coding_worker = spec["worker_class"] in {"code_small", "code_heavy"}
        if sandbox_worker:
            if os.getenv("SANDBOX_CODING_ENABLED", "0") != "1":
                reasons.append("CODING_SANDBOX_DISABLED")
            else:
                if not _valid_runtime_secret("SANDBOX_BROKER_SECRET"):
                    reasons.append("SANDBOX_BROKER_SECRET_INVALID")
                if coding_worker and not _valid_runtime_secret("SANDBOX_TOKEN_SECRET"):
                    reasons.append("SANDBOX_TOKEN_SECRET_INVALID")
        if spec["requires_genx"] and not os.getenv("GENX_API_KEY", "").strip():
            reasons.append("GENX_NOT_CONFIGURED")
        elif spec["requires_genx"]:
            keywords, fallback_category = genx_requirements[spec["worker_class"]]
            if not catalog_supports(*keywords, fallback_category=fallback_category):
                reasons.append("GENX_CAPABILITY_UNAVAILABLE")
        enablement = {"production_enabled": not reasons, "enablement_reason_codes": reasons}
        matching = [worker for worker in runtime.values() if worker.worker_class == spec["worker_class"]]
        if not matching:
            rows.append({**spec, **enablement, "id": "not-started", "status": "OFFLINE", "node": "—", "current_job": None, "last_heartbeat": None})
        for worker in matching:
            rows.append({
                **spec,
                **enablement,
                "id": worker.id,
                "version": worker.version,
                "status": worker.status,
                "node": worker.node,
                "current_job": str(worker.current_job_id) if worker.current_job_id else None,
                "last_heartbeat": _dt(worker.last_heartbeat),
            })
    unknown = [worker for worker in runtime.values() if worker.worker_class not in {spec["worker_class"] for spec in manifest}]
    for worker in unknown:
        rows.append({
            "worker_class": worker.worker_class,
            "version": worker.version,
            "operations": [],
            "qa_profile": "UNKNOWN",
            "description": "Runtime worker not present in current registry",
            "production_enabled": False,
            "enablement_reason_codes": ["WORKER_NOT_REGISTERED"],
            "id": worker.id,
            "status": worker.status,
            "node": worker.node,
            "current_job": str(worker.current_job_id) if worker.current_job_id else None,
            "last_heartbeat": _dt(worker.last_heartbeat),
        })
    return {"section": "agents", "rows": rows, "meta": {"registry_count": len(manifest), "runtime_count": len(runtime)}}


def markets_snapshot() -> dict:
    rows = []
    for market in Marketplace.objects.order_by("slug"):
        try:
            health = market.health_snapshot
        except Exception:
            health = None
        policy = market.policy_versions.order_by("-checked_at", "-created_at").first()
        try:
            profile = market.integration_profile
        except Exception:
            profile = None
        preflight = AcquisitionPreflight.objects.filter(job__marketplace=market).order_by("-created_at").first()
        blockers = []
        if not market.enabled:
            blockers.append("MARKET_DISABLED")
        if market.status != Marketplace.Status.LIVE:
            blockers.append("MARKET_NOT_LIVE")
        if not market.payout_ready:
            blockers.append("PAYOUT_NOT_READY")
        if not market.south_africa_verified:
            blockers.append("SOUTH_AFRICA_NOT_VERIFIED")
        if policy is None or not policy.automation_allowed:
            blockers.append("AUTOMATION_POLICY_NOT_APPROVED")
        if profile:
            blockers.extend(profile.blockers)
        if preflight and not preflight.allowed:
            blockers.extend(preflight.reason_codes)
        rows.append({
            "market": market.slug,
            "status": market.status,
            "enabled": market.enabled,
            "payout_ready": market.payout_ready,
            "south_africa_verified": market.south_africa_verified,
            "fee_rate": _dec(market.fee_rate),
            "autonomy_mode": current_mode().value,
            "auto_acquisition_switch": (
                os.getenv("AGENTGIGS_AUTO_APPLY_ENABLED", "0") == "1" if market.slug == "agentgigs"
                else os.getenv(f"{market.slug.upper().replace('-', '_')}_AUTO_ACQUIRE_ENABLED", "0") == "1"
            ),
            "adapter": profile.adapter_name if profile else "",
            "adapter_version": profile.adapter_version if profile else "",
            "source_wired": profile.source_wired if profile else False,
            "adapter_capabilities": profile.capabilities if profile else {},
            "adapter_sources": profile.source_urls if profile else [],
            "adapter_docs_checked": _dt(profile.docs_checked_at) if profile else None,
            "adapter_policy_verified": profile.policy_verified if profile else False,
            "adapter_acquisition_enabled": profile.autonomous_acquisition_enabled if profile else False,
            "rate_limit": profile.rate_limit if profile else "",
            "payout_method": profile.payout_method if profile else market.payment_model,
            "policy_automation_allowed": policy.automation_allowed if policy else None,
            "policy_webdock_compatible": policy.webdock_compatible if policy else None,
            "policy_checked": _dt(policy.checked_at) if policy else None,
            "blockers": list(dict.fromkeys(blockers)),
            "api": health.api_ok if health else None,
            "auth": health.auth_ok if health else None,
            "payout": health.payout_ok if health else None,
            "supply": health.supply_ok if health else None,
            "last_error": health.last_error_code if health else "",
            "checked": _dt(health.checked_at) if health else None,
            "jobs_total": Job.objects.filter(marketplace=market).count(),
            "opportunities_seen_24h": Job.objects.filter(marketplace=market, created_at__gte=timezone.now() - timedelta(hours=24)).count(),
            "applications_total": Application.objects.filter(job__marketplace=market).count(),
            "unknown_remote_state": Application.objects.filter(job__marketplace=market, status="UNKNOWN_REMOTE_STATE").count(),
            "latest_preflight": "ALLOWED" if preflight and preflight.allowed else "BLOCKED" if preflight else "NO DECISION",
            "latest_preflight_reasons": preflight.reason_codes if preflight else [],
        })
    return {"section": "markets", "rows": rows}


def earnings_snapshot(limit: int = 100) -> dict:
    rows = [{
        "job": str(row.job_id),
        "state": row.state,
        "gross": f"{row.currency} {row.gross}",
        "fee": f"{row.currency} {row.fee}",
        "net": f"{row.currency} {row.net}",
        "earned": _dt(row.earned_at),
        "pending": _dt(row.pending_at),
        "settled": _dt(row.settled_at),
        "reference": row.external_reference,
    } for row in Payout.objects.order_by("-updated_at")[:limit]]
    return {"section": "earnings", "rows": rows}


def treasury_snapshot(limit: int = 100) -> dict:
    balances = [{
        "account": row.account,
        "market": row.marketplace.slug if row.marketplace else "global",
        "currency": row.currency,
        "earned": _dec(row.earned),
        "pending": _dec(row.pending),
        "settled": _dec(row.settled),
    } for row in TreasuryBalance.objects.select_related("marketplace").order_by("account", "currency")]
    ledger = [{
        "created": _dt(row.created_at),
        "event": row.event_type,
        "reference": row.reference,
        "account": row.account,
        "counter_account": row.counter_account,
        "amount": f"{row.currency} {row.amount}",
    } for row in LedgerEntry.objects.order_by("-created_at")[:limit]]
    return {"section": "treasury", "rows": balances, "secondary_rows": ledger}


def genx_snapshot(limit: int = 100) -> dict:
    latest = GenXAccountSnapshot.objects.order_by("-created_at").first()
    rows = [{
        "created": _dt(row.created_at),
        "job": str(row.job_id) if row.job_id else None,
        "worker": row.worker_id,
        "model": row.model,
        "task_class": row.task_class,
        "status": row.status,
        "estimated_credits": _dec(row.estimated_credits),
        "credits": _dec(row.credits),
        "latency_ms": row.latency_ms,
        "error": row.error_code,
    } for row in GenXCall.objects.order_by("-created_at")[:limit]]
    return {"section": "genx", "rows": rows, "meta": {"available_credits": _dec(latest.available_credits) if latest else None, "snapshot_at": _dt(latest.created_at) if latest else None}}


def nodes_snapshot() -> dict:
    rows = [{
        "node": row.id,
        "hostname": row.hostname,
        "release": row.release_version,
        "role": row.role_profile,
        "health": row.health,
        "cpu_percent": _dec(row.cpu_percent),
        "ram_percent": _dec(row.ram_percent),
        "disk_percent": _dec(row.disk_percent),
        "last_heartbeat": _dt(row.last_heartbeat),
    } for row in Node.objects.order_by("id")]
    heartbeats = [{"service": row.service, "node": row.node_id, "last_seen": _dt(row.last_seen_at), "details": row.details} for row in ServiceHeartbeat.objects.order_by("service")]
    return {"section": "nodes", "rows": rows, "secondary_rows": heartbeats}


def storage_snapshot() -> dict:
    rows = [
        _disk_row("Jobs", os.getenv("AMARKTAI_JOB_ROOT", "/var/lib/amarktai-earn/jobs")),
        _disk_row("Uploads", os.getenv("AMARKTAI_UPLOAD_ROOT", "/var/lib/amarktai-earn/uploads")),
        _disk_row("Artifacts", "/var/lib/amarktai-earn/artifacts"),
        _disk_row("Backups", "/var/lib/amarktai-earn/backups"),
    ]
    latest = ResourceSnapshot.objects.order_by("-created_at").first()
    blockers = list(AdmissionDecision.objects.filter(allowed=False).order_by("-created_at").values("purpose", "operation", "reason_codes", "created_at")[:20])
    return {
        "section": "storage",
        "rows": rows,
        "secondary_rows": blockers,
        "meta": None if latest is None else {
            "healthy": latest.healthy,
            "disk_free_bytes": latest.disk_free_bytes,
            "disk_free_percent": _dec(latest.disk_free_percent),
            "memory_available_bytes": latest.memory_available_bytes,
            "load_per_cpu": _dec(latest.load_per_cpu),
            "storage_usage": latest.storage_usage,
            "queue_pressure": latest.queue_pressure,
            "blockers": latest.blocker_codes,
            "captured": _dt(latest.created_at),
        },
    }


def performance_snapshot() -> dict:
    since = timezone.now() - timedelta(days=7)
    executions = Execution.objects.filter(created_at__gte=since)
    qa = QAResult.objects.filter(created_at__gte=since)
    grouped = list(executions.values("worker__worker_class", "status").annotate(count=Count("id")).order_by("worker__worker_class", "status"))
    qa_total = qa.count()
    qa_pass = qa.filter(passed=True).count()
    models = [{
        "model": row.model,
        "task_class": row.task_class,
        "attempts": row.attempts,
        "accepted": row.accepted,
        "credits": _dec(row.credits),
        "profit": _dec(row.profit),
        "avg_latency_ms": 0 if not row.attempts else round(row.total_latency_ms / row.attempts),
    } for row in ModelStat.objects.order_by("-attempts")[:50]]
    return {
        "section": "performance",
        "rows": [{"worker_class": row["worker__worker_class"] or "unassigned", "status": row["status"], "count": row["count"]} for row in grouped],
        "secondary_rows": models,
        "meta": {"window": "7d", "qa_total": qa_total, "qa_pass": qa_pass, "qa_pass_rate": None if qa_total == 0 else round((qa_pass / qa_total) * 100, 2)},
    }


def logs_snapshot(limit: int = 200) -> dict:
    rows = [{
        "created": _dt(row.created_at),
        "severity": row.severity,
        "event": row.event_type,
        "actor": row.actor,
        "correlation": str(row.correlation_id),
        "metadata": row.metadata,
    } for row in AuditEvent.objects.order_by("-created_at")[:limit]]
    return {"section": "logs", "rows": rows}


def alerts_snapshot(limit: int = 200) -> dict:
    rows = [{
        "created": _dt(row.created_at),
        "severity": row.severity,
        "type": row.alert_type,
        "status": row.status,
        "message": row.message,
        "acknowledged": _dt(row.acknowledged_at),
        "resolved": _dt(row.resolved_at),
    } for row in Alert.objects.order_by("-created_at")[:limit]]
    return {"section": "alerts", "rows": rows}


def settings_snapshot() -> dict:
    rows = []
    for row in SystemSetting.objects.order_by("key"):
        rows.append({
            "key": row.key,
            "value": "CONFIGURED — HIDDEN" if row.sensitive else row.value,
            "sensitive": row.sensitive,
            "updated": _dt(row.updated_at),
        })
    return {"section": "settings", "rows": rows, "meta": {"autonomous_mode": current_mode().value}}


def security_snapshot(owner) -> dict:
    profile = OwnerSecurityProfile.objects.filter(user=owner).first()
    now = timezone.now()
    active_refresh = RefreshSession.objects.filter(user=owner, revoked_at__isnull=True, expires_at__gt=now).count()
    recovery_remaining = RecoveryCode.objects.filter(user=owner, used_at__isnull=True).count()
    active_lockouts = AuthThrottle.objects.filter(locked_until__gt=now).count()
    active_reauth = ReauthenticationGrant.objects.filter(user=owner, used_at__isnull=True, revoked_at__isnull=True, expires_at__gt=now).count()
    configured_market_credentials = MarketplaceCredential.objects.filter(active=True).count()
    hidden = "CONFIGURED — HIDDEN"

    def secret_state(name: str) -> str:
        return hidden if os.getenv(name, "").strip() else "NOT CONFIGURED"

    rows = [{
        "created": _dt(row.created_at),
        "severity": row.severity,
        "event": row.event_type,
        "actor": row.actor,
    } for row in AuditEvent.objects.filter(Q(event_type__startswith="auth.") | Q(event_type__startswith="security.")).order_by("-created_at")[:100]]
    return {
        "section": "security",
        "cards": [
            {"label": "TOTP", "value": "ENROLLED" if profile and profile.totp_confirmed_at else "NOT ENROLLED"},
            {"label": "SECURITY VERSION", "value": profile.security_version if profile else 0},
            {"label": "ACTIVE REFRESH SESSIONS", "value": active_refresh},
            {"label": "RECOVERY CODES REMAINING", "value": recovery_remaining},
            {"label": "ACTIVE AUTH COOLDOWNS", "value": active_lockouts},
            {"label": "ACTIVE REAUTH GRANTS", "value": active_reauth},
            {"label": "JWT SIGNING KEYS", "value": secret_state("JWT_SIGNING_KEYS_JSON")},
            {"label": "FIELD ENCRYPTION KEYS", "value": secret_state("FIELD_ENCRYPTION_KEYS_JSON")},
            {"label": "GENX MASTER CREDENTIAL", "value": secret_state("GENX_API_KEY")},
            {"label": "MARKET WEBHOOK SECRET", "value": secret_state("AGENTGIGS_WEBHOOK_SECRET")},
            {"label": "BACKUP PASSPHRASE", "value": secret_state("BACKUP_PASSPHRASE")},
            {"label": "MARKETPLACE CREDENTIALS", "value": f"{configured_market_credentials} {hidden}" if configured_market_credentials else "NOT CONFIGURED"},
        ],
        "rows": rows,
    }


def snapshot(section: str, owner=None) -> dict:
    section = section.strip().lower()
    if section not in SECTIONS:
        raise KeyError(section)
    if section == "overview": return overview_snapshot()
    if section == "live-work": return live_work_snapshot()
    if section == "agents": return agents_snapshot()
    if section == "markets": return markets_snapshot()
    if section == "earnings": return earnings_snapshot()
    if section == "treasury": return treasury_snapshot()
    if section == "genx": return genx_snapshot()
    if section == "nodes": return nodes_snapshot()
    if section == "storage": return storage_snapshot()
    if section == "performance": return performance_snapshot()
    if section == "logs": return logs_snapshot()
    if section == "alerts": return alerts_snapshot()
    if section == "settings": return settings_snapshot()
    if section == "security": return security_snapshot(owner)
    raise KeyError(section)
