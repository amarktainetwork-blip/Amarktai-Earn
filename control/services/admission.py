from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from control.models import AdmissionDecision, Alert, AuditEvent, Execution, GenXCall, Job, ResourceSnapshot
from planning.models import WorkPlan
from workers.registry import WorkerRegistryError, operation_spec


class AdmissionDenied(RuntimeError):
    def __init__(self, reason_codes: list[str]):
        self.reason_codes = reason_codes
        super().__init__("admission blocked: " + ",".join(reason_codes))


@dataclass(frozen=True)
class ResourceMetrics:
    disk_free_bytes: int
    disk_free_percent: Decimal
    memory_available_bytes: int | None
    load_per_cpu: Decimal | None
    storage_usage: dict[str, int]
    queue_pressure: dict[str, int]


STORAGE_CLASSES = {
    "uploads": ("AMARKTAI_UPLOAD_ROOT", "/var/lib/amarktai-earn/uploads", "AMARKTAI_UPLOAD_QUOTA_BYTES", 5 * 1024**3),
    "jobs": ("AMARKTAI_JOB_ROOT", "/var/lib/amarktai-earn/jobs", "AMARKTAI_JOB_QUOTA_BYTES", 10 * 1024**3),
    "repositories": ("AMARKTAI_REPO_ROOT", "/var/lib/amarktai-earn/repos", "AMARKTAI_REPO_QUOTA_BYTES", 5 * 1024**3),
    "artifacts": ("AMARKTAI_ARTIFACT_ROOT", "/var/lib/amarktai-earn/artifacts", "AMARKTAI_ARTIFACT_QUOTA_BYTES", 10 * 1024**3),
    "logs": ("AMARKTAI_LOG_ROOT", "/var/lib/amarktai-earn/logs", "AMARKTAI_LOG_QUOTA_BYTES", 1024**3),
    "cache": ("AMARKTAI_CACHE_ROOT", "/var/lib/amarktai-earn/cache", "AMARKTAI_CACHE_QUOTA_BYTES", 2 * 1024**3),
}


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


def _decimal_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except Exception:
        return Decimal(default)


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _directory_usage(root: Path, *, max_entries: int) -> int:
    if not root.exists() or root.is_symlink():
        return 0
    total = 0
    seen = 0
    for base, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not (Path(base) / name).is_symlink()]
        for name in files:
            seen += 1
            if seen > max_entries:
                return total
            path = Path(base) / name
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode):
                    total += max(0, info.st_size)
            except OSError:
                continue
    return total


def _memory_available() -> int | None:
    try:
        maximum = Path("/sys/fs/cgroup/memory.max").read_text(encoding="ascii").strip()
        current = int(Path("/sys/fs/cgroup/memory.current").read_text(encoding="ascii").strip())
        if maximum != "max":
            return max(0, int(maximum) - current)
    except (OSError, ValueError):
        pass
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong), ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.available_physical)
        except (AttributeError, OSError):
            pass
    return None


def _load_per_cpu() -> Decimal | None:
    try:
        return (Decimal(str(os.getloadavg()[0])) / Decimal(max(1, os.cpu_count() or 1))).quantize(Decimal("0.001"))
    except (AttributeError, OSError):
        return None


def _queue_pressure() -> dict[str, int]:
    return {
        "queued_plans": WorkPlan.objects.filter(status=WorkPlan.Status.QUEUED).count(),
        "active_executions": Execution.objects.filter(status="EXECUTING").count(),
        "code_sandboxes": Execution.objects.filter(status="EXECUTING", worker__worker_class__in=["code_small", "code_heavy", "ci_testing"]).count(),
        "genx_jobs": GenXCall.objects.filter(status__in=["RESERVED", "SUBMITTING", "SUBMITTED", "UNKNOWN_REMOTE_STATE"]).count(),
        "media_processes": Execution.objects.filter(status="EXECUTING", worker__worker_class="media").count(),
    }


def collect_metrics() -> ResourceMetrics:
    roots = {name: Path(os.getenv(env_name, default)).resolve() for name, (env_name, default, _quota, _limit) in STORAGE_CLASSES.items()}
    disk_rows = []
    for root in roots.values():
        try:
            disk_rows.append(shutil.disk_usage(_nearest_existing(root)))
        except OSError:
            continue
    if disk_rows:
        free = min(row.free for row in disk_rows)
        free_percent = min(Decimal(row.free * 100) / Decimal(max(1, row.total)) for row in disk_rows)
    else:
        free = 0
        free_percent = Decimal("0")
    max_entries = _int_env("AMARKTAI_STORAGE_SCAN_MAX_ENTRIES", 100000, minimum=1000)
    usage = {name: _directory_usage(root, max_entries=max_entries) for name, root in roots.items()}
    return ResourceMetrics(
        disk_free_bytes=free,
        disk_free_percent=free_percent.quantize(Decimal("0.01")),
        memory_available_bytes=_memory_available(),
        load_per_cpu=_load_per_cpu(),
        storage_usage=usage,
        queue_pressure=_queue_pressure(),
    )


def _metrics(value: ResourceMetrics | dict[str, Any] | None) -> ResourceMetrics:
    if isinstance(value, ResourceMetrics):
        return value
    if isinstance(value, dict):
        return ResourceMetrics(
            disk_free_bytes=int(value.get("disk_free_bytes", 0)),
            disk_free_percent=Decimal(str(value.get("disk_free_percent", 0))),
            memory_available_bytes=None if value.get("memory_available_bytes") is None else int(value["memory_available_bytes"]),
            load_per_cpu=None if value.get("load_per_cpu") is None else Decimal(str(value["load_per_cpu"])),
            storage_usage={str(k): int(v) for k, v in dict(value.get("storage_usage", {})).items()},
            queue_pressure={str(k): int(v) for k, v in dict(value.get("queue_pressure", {})).items()},
        )
    return collect_metrics()


def decide_admission(
    *,
    purpose: str,
    job: Job | None = None,
    operation: str = "",
    expected_storage_bytes: int = 0,
    metrics: ResourceMetrics | dict[str, Any] | None = None,
    persist: bool = True,
) -> AdmissionDecision | Any:
    purpose = purpose.strip().upper()[:40]
    operation = operation.strip()[:80]
    current = _metrics(metrics)
    reasons: list[str] = []
    details: dict[str, Any] = {"expected_storage_bytes": max(0, int(expected_storage_bytes))}

    if current.disk_free_bytes < _int_env("AMARKTAI_MIN_FREE_DISK_BYTES", 2 * 1024**3):
        reasons.append("DISK_FREE_BYTES_CRITICAL")
    if current.disk_free_percent < _decimal_env("AMARKTAI_MIN_FREE_DISK_PERCENT", "10"):
        reasons.append("DISK_FREE_PERCENT_CRITICAL")
    if current.disk_free_bytes < max(0, int(expected_storage_bytes)) + _int_env("AMARKTAI_STORAGE_RESERVE_BYTES", 512 * 1024**2):
        reasons.append("JOB_STORAGE_ESTIMATE_EXCEEDS_HEADROOM")
    for name, (_env_name, _default, quota_env, default_quota) in STORAGE_CLASSES.items():
        if current.storage_usage.get(name, 0) >= _int_env(quota_env, default_quota, minimum=1):
            reasons.append(f"{name.upper()}_QUOTA_EXCEEDED")

    memory_required = _int_env(
        "AMARKTAI_LARGE_JOB_MEMORY_HEADROOM_BYTES" if purpose in {"SANDBOX", "MEDIA"} else "AMARKTAI_MIN_MEMORY_HEADROOM_BYTES",
        1024**3 if purpose in {"SANDBOX", "MEDIA"} else 512 * 1024**2,
    )
    test_environment = os.getenv("AMARKTAI_ENV", "development") == "test"
    if current.memory_available_bytes is None:
        if not test_environment:
            reasons.append("MEMORY_HEADROOM_UNAVAILABLE")
    elif current.memory_available_bytes < memory_required:
        reasons.append("MEMORY_HEADROOM_LOW")
    if current.load_per_cpu is None:
        if not test_environment:
            reasons.append("CPU_LOAD_UNAVAILABLE")
    elif current.load_per_cpu > _decimal_env("AMARKTAI_MAX_LOAD_PER_CPU", "1.50"):
        reasons.append("CPU_LOAD_HIGH")

    limits = {
        "queued_plans": ("MAX_QUEUED_WORKPLANS", 100, "QUEUED_WORKPLAN_LIMIT"),
        "active_executions": ("MAX_ACTIVE_JOBS", 4, "ACTIVE_EXECUTION_LIMIT"),
        "code_sandboxes": ("MAX_ACTIVE_CODE_SANDBOXES", 1, "CODE_SANDBOX_LIMIT"),
        "genx_jobs": ("MAX_ACTIVE_GENX_JOBS", 2, "GENX_CONCURRENCY_LIMIT"),
        "media_processes": ("MAX_ACTIVE_MEDIA_PROCESSES", 1, "MEDIA_CONCURRENCY_LIMIT"),
    }
    for key, (env_name, default, code) in limits.items():
        pressure_value = current.queue_pressure.get(key, 0)
        if job is not None and purpose == "SANDBOX" and key in {"active_executions", "code_sandboxes"}:
            pressure_value = max(0, pressure_value - Execution.objects.filter(job=job, status="EXECUTING").count())
        if pressure_value >= _int_env(env_name, default, minimum=1):
            if key == "code_sandboxes" and purpose != "SANDBOX":
                continue
            if key == "genx_jobs" and purpose not in {"GENX", "SANDBOX"}:
                continue
            if key == "media_processes" and purpose != "MEDIA":
                continue
            reasons.append(code)

    spec = None
    if operation:
        try:
            spec = operation_spec(operation)
        except WorkerRegistryError:
            reasons.append("WORKER_OPERATION_NOT_REGISTERED")
        if spec:
            disabled = {item.strip() for item in os.getenv("WORKER_DISABLED_CLASSES", "").split(",") if item.strip()}
            if spec.worker_class in disabled:
                reasons.append("WORKER_DISABLED")
            if not spec.qa_profile:
                reasons.append("QA_PROFILE_NOT_REGISTERED")
            for command in spec.runtime_commands:
                if not shutil.which(command):
                    reasons.append(f"{command.upper()}_RUNTIME_UNAVAILABLE")
            if spec.worker_class in {"code_small", "code_heavy", "ci_testing"} and os.getenv("SANDBOX_CODING_ENABLED", "0") != "1":
                reasons.append("CODING_SANDBOX_DISABLED")

    if job is not None:
        active_market = Execution.objects.filter(job__marketplace=job.marketplace, status="EXECUTING").exclude(job=job).count()
        if active_market >= _int_env("MAX_ACTIVE_JOBS_PER_MARKET", 2, minimum=1):
            reasons.append("MARKET_CONCURRENCY_LIMIT")
        score = getattr(job, "jobscore", None)
        if purpose == "ACQUISITION":
            if not job.marketplace.enabled:
                reasons.append("MARKET_DISABLED")
            if job.marketplace.status != job.marketplace.Status.LIVE:
                reasons.append("MARKET_NOT_LIVE")
            if not job.marketplace.payout_ready:
                reasons.append("PAYOUT_NOT_READY")
            if not job.marketplace.south_africa_verified:
                reasons.append("SOUTH_AFRICA_NOT_VERIFIED")
            if score is None or Decimal(str(score.expected_profit)) <= 0:
                reasons.append("EXPECTED_PROFIT_NOT_POSITIVE")
        needs_genx_budget = purpose == "GENX" or (purpose == "SANDBOX" and spec is not None and spec.requires_genx)
        if needs_genx_budget and (score is None or Decimal(str(score.max_genx_credits)) <= 0):
            reasons.append("GENX_JOB_BUDGET_NOT_POSITIVE")

    reasons = list(dict.fromkeys(reasons))
    snapshot = None
    if persist:
        snapshot = ResourceSnapshot.objects.create(
            node_id=os.getenv("NODE_ID", "VPS1")[:120],
            purpose=purpose,
            disk_free_bytes=max(0, current.disk_free_bytes),
            disk_free_percent=max(Decimal("0"), current.disk_free_percent),
            memory_available_bytes=max(0, current.memory_available_bytes or 0),
            load_per_cpu=max(Decimal("0"), current.load_per_cpu or 0),
            storage_usage=current.storage_usage,
            queue_pressure=current.queue_pressure,
            healthy=not reasons,
            blocker_codes=reasons,
        )
        decision = AdmissionDecision.objects.create(
            job=job,
            snapshot=snapshot,
            purpose=purpose,
            operation=operation,
            allowed=not reasons,
            reason_codes=reasons,
            details=details,
        )
        AuditEvent.objects.create(
            severity="INFO" if decision.allowed else "WARNING",
            event_type="operations.admission_allowed" if decision.allowed else "operations.admission_blocked",
            actor="resource-governor",
            metadata={"decision_id": str(decision.id), "job_id": str(job.id) if job else None, "purpose": purpose, "operation": operation, "reason_codes": reasons},
        )
        if not decision.allowed and (purpose == "ACQUISITION" or any(code.startswith(("DISK_", "MEMORY_", "CPU_")) for code in reasons)):
            Alert.objects.create(
                severity="CRITICAL" if any(code.startswith("DISK_") for code in reasons) else "WARNING",
                alert_type="ADMISSION_BLOCKED",
                message=f"{purpose} was blocked by the centralized resource governor.",
                metadata={"decision_id": str(decision.id), "job_id": str(job.id) if job else None, "reason_codes": reasons},
            )
        return decision
    return type("AdmissionResult", (), {"allowed": not reasons, "reason_codes": reasons, "details": details})()


def require_admission(**kwargs) -> AdmissionDecision | Any:
    decision = decide_admission(**kwargs)
    if not decision.allowed:
        raise AdmissionDenied(list(decision.reason_codes))
    return decision
