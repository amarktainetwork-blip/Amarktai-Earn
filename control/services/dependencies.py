from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from control.models import AuditEvent
from planning.models import DependencyPreparation, RepositorySnapshot
from sandbox_broker.client import SandboxBrokerClient, SandboxBrokerError


class DependencyPreparationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DependencyRequest:
    ecosystem: str
    manifest_path: str
    manifest_hash: str


_PINNED_HASHED = re.compile(
    r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+"
    r"(?:\s+--hash=sha256:[0-9a-fA-F]{64})+"
)


def _inside(path: Path, root: Path) -> bool:
    resolved, base = path.resolve(), root.resolve()
    return resolved == base or base in resolved.parents


def _bounded(path: Path) -> bytes:
    maximum = max(1024, min(int(os.getenv("DEPENDENCY_MAX_MANIFEST_BYTES", "1048576")), 8 * 1024 * 1024))
    if not path.is_file() or path.is_symlink():
        raise DependencyPreparationError("DEPENDENCY_MANIFEST_INVALID")
    if path.stat().st_size > maximum:
        raise DependencyPreparationError("DEPENDENCY_MANIFEST_TOO_LARGE")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DependencyPreparationError("DEPENDENCY_MANIFEST_INVALID") from exc


def _python_lock_valid(raw: bytes) -> bool:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False
    requirements: list[str] = []
    current = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continued = stripped.endswith("\\")
        fragment = stripped[:-1].rstrip() if continued else stripped
        current = f"{current} {fragment}".strip()
        if not continued:
            requirements.append(current)
            current = ""
    if current:
        return False
    return bool(requirements) and all(_PINNED_HASHED.fullmatch(line) for line in requirements)


def inspect_dependency_request(snapshot: RepositorySnapshot) -> tuple[DependencyRequest | None, list[str]]:
    root = Path(snapshot.path).resolve()
    configured = Path(os.getenv("AMARKTAI_REPO_ROOT", "/var/lib/amarktai-earn/repos")).resolve()
    if not _inside(root, configured) or not root.is_dir():
        return None, ["REPOSITORY_SNAPSHOT_PATH_INVALID"]

    requirements = root / "requirements.txt"
    package = root / "package.json"
    package_lock = root / "package-lock.json"
    pyproject = root / "pyproject.toml"
    if requirements.exists() and (package.exists() or package_lock.exists()):
        return None, ["MULTIPLE_DEPENDENCY_ECOSYSTEMS_UNSUPPORTED"]
    if requirements.exists():
        try:
            raw = _bounded(requirements)
        except DependencyPreparationError as exc:
            return None, [str(exc)]
        if not _python_lock_valid(raw):
            return None, ["PYTHON_REQUIREMENTS_NOT_HASH_LOCKED"]
        return DependencyRequest("python", "requirements.txt", hashlib.sha256(raw).hexdigest()), []
    if package.exists() or package_lock.exists():
        if not package.is_file() or not package_lock.is_file() or package.is_symlink() or package_lock.is_symlink():
            return None, ["NODE_LOCKFILE_REQUIRED"]
        try:
            package_raw, lock_raw = _bounded(package), _bounded(package_lock)
        except DependencyPreparationError as exc:
            return None, [str(exc)]
        try:
            package_data, lock_data = json.loads(package_raw), json.loads(lock_raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, ["NODE_MANIFEST_INVALID"]
        if not isinstance(package_data, dict) or not isinstance(lock_data, dict) or int(lock_data.get("lockfileVersion", 0)) not in {2, 3}:
            return None, ["NODE_LOCKFILE_UNSUPPORTED"]
        digest = hashlib.sha256(package_raw + b"\0" + lock_raw).hexdigest()
        return DependencyRequest("node", "package-lock.json", digest), []
    if pyproject.exists():
        return None, ["PYTHON_LOCKFILE_REQUIRED"]
    return None, []


def prepare_dependencies(*, job, snapshot: RepositorySnapshot, request: DependencyRequest, broker: SandboxBrokerClient | None = None) -> tuple[DependencyPreparation, str]:
    row, _ = DependencyPreparation.objects.get_or_create(
        job=job,
        repository_snapshot=snapshot,
        ecosystem=request.ecosystem,
        manifest_hash=request.manifest_hash,
        defaults={"manifest_path": request.manifest_path},
    )
    if os.getenv("DEPENDENCY_PREPARATION_ENABLED", "0") != "1":
        row.status = DependencyPreparation.Status.BLOCKED
        row.reason_codes = ["DEPENDENCY_PREPARATION_DISABLED"]
        row.save(update_fields=["status", "reason_codes", "updated_at"])
        raise DependencyPreparationError("dependency preparation is disabled")
    try:
        result = (broker or SandboxBrokerClient()).prepare({
            "snapshot_rel": str(Path(snapshot.path).resolve().relative_to(Path(os.getenv("AMARKTAI_REPO_ROOT", "/var/lib/amarktai-earn/repos")).resolve())).replace("\\", "/"),
            "ecosystem": request.ecosystem,
            "manifest_path": request.manifest_path,
            "manifest_hash": request.manifest_hash,
        })
    except (SandboxBrokerError, ValueError) as exc:
        row.status = DependencyPreparation.Status.FAILED
        row.reason_codes = ["DEPENDENCY_PREPARATION_FAILED"]
        row.details = {"error_code": exc.__class__.__name__}
        row.save()
        raise DependencyPreparationError(str(exc)) from exc
    row.status = DependencyPreparation.Status.READY
    row.cache_key = str(result.get("cache_key") or "")[:100]
    row.file_count = max(0, int(result.get("file_count") or 0))
    row.total_bytes = max(0, int(result.get("total_bytes") or 0))
    row.reason_codes = []
    row.details = {"cache_hit": bool(result.get("cache_hit"))}
    row.save()
    AuditEvent.objects.create(
        event_type="job.dependencies_prepared", actor="dependency-controller",
        metadata={"job_id": str(job.id), "snapshot_id": snapshot.id, "ecosystem": request.ecosystem, "manifest_hash": request.manifest_hash, "cache_key": row.cache_key, "file_count": row.file_count, "total_bytes": row.total_bytes},
    )
    return row, row.cache_key
