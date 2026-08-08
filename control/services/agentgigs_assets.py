from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests
from django.db.models import Sum
from control.models import AuditEvent, Job
from markets.agentgigs.assets import (
    DEFAULT_MAX_SOURCE_BYTES,
    RemoteAssetRef,
    RemoteAssetSafetyError,
    assert_public_https_url,
    extract_source_asset_refs,
    positive_int,
    safe_filename,
    supported_source_name,
)
from markets.agentgigs.client import AgentGigsAdapter, AgentGigsError
from planning.models import JobAsset
from planning.asset_policy import AssetPolicyError, validate_role
from planning.services import rebuild_asset_manifest, stage_local_job_asset


class AssetIngestionError(RuntimeError):
    pass


MAX_REDIRECTS = 3


def _download_signed_asset(ref: RemoteAssetRef, target: Path, max_bytes: int) -> tuple[int, str]:
    session = requests.Session()
    current = ref.url
    response = None
    for _ in range(MAX_REDIRECTS + 1):
        assert_public_https_url(current)
        try:
            response = session.get(
                current,
                headers={"Accept": "application/octet-stream"},
                timeout=(5, 30),
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise AssetIngestionError(f"remote asset download failed: {exc.__class__.__name__}") from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise AssetIngestionError("remote asset redirect had no location")
            current = urljoin(current, location)
            continue
        break
    else:
        raise AssetIngestionError("remote asset exceeded redirect limit")

    assert response is not None
    try:
        if not response.ok:
            raise AssetIngestionError(f"remote asset HTTP {response.status_code}")
        declared = positive_int(response.headers.get("Content-Length"))
        if declared and declared > max_bytes:
            raise AssetIngestionError("remote asset exceeds configured size limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.part")
        total = 0
        try:
            with temp.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise AssetIngestionError("remote asset exceeds configured size limit")
                    handle.write(chunk)
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        return total, content_type
    finally:
        response.close()


def _blocked_asset(job: Job, ref: RemoteAssetRef, error_code: str) -> JobAsset:
    asset, _ = JobAsset.objects.update_or_create(
        job=job,
        external_id=ref.external_id,
        defaults={
            "semantic_role": "other-approved",
            "source": f"agentgigs_{ref.source_kind}"[:40],
            "name": safe_filename(ref.name, ref.external_id),
            "path": "",
            "url": "",
            "sha256": "",
            "size_bytes": ref.size_bytes,
            "mime_type": ref.mime_type[:120],
            "status": JobAsset.Status.BLOCKED,
            "verified_at": None,
            "metadata": {"reason_codes": [error_code]},
        },
    )
    AuditEvent.objects.create(
        severity="WARNING",
        event_type="job.asset_blocked",
        actor="agentgigs-asset-ingestor",
        metadata={
            "job_id": str(job.id),
            "asset_id": asset.id,
            "source_kind": ref.source_kind,
            "error_code": error_code,
        },
    )
    rebuild_asset_manifest(job)
    return asset


def _asset_role(job: Job, ref: RemoteAssetRef, total_refs: int) -> str:
    if ref.semantic_role:
        return validate_role(ref.semantic_role)
    payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    mapping = payload.get("asset_roles") if isinstance(payload.get("asset_roles"), dict) else {}
    for key in (ref.external_id, ref.name):
        if key in mapping:
            return validate_role(str(mapping[key]))
    if total_refs == 1:
        return "source"
    raise AssetPolicyError("ASSET_ROLE_AMBIGUOUS")


def ingest_agentgigs_assets(
    job: Job,
    adapter: AgentGigsAdapter,
    *,
    details: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    fetcher: Callable[[RemoteAssetRef, Path, int], tuple[int, str]] | None = None,
) -> dict[str, int]:
    """Copy AgentGigs source attachments into protected local storage and verify them.

    Signed remote URLs are transient and never persisted as worker inputs. Only
    local files that have been size-bounded, stored under the approved upload
    root, and SHA-256 hashed become VERIFIED JobAsset rows.
    """
    if job.state not in {Job.State.CLAIMED, Job.State.AWARDED}:
        return {"found": 0, "ingested": 0, "existing": 0, "blocked": 0, "failed": 0}

    opportunity = adapter.normalize_job(job.normalized_payload)
    adapter.ensure_nda(opportunity)
    if details is None:
        details = adapter.get_status(opportunity)
    if messages is None:
        messages = adapter.get_messages(opportunity)

    refs = extract_source_asset_refs(details, messages)
    maximum_files = max(1, int(os.getenv("JOB_ASSET_MAX_FILES", "12")))
    maximum_total = max(1, int(os.getenv("JOB_ASSET_MAX_TOTAL_BYTES", str(250 * 1024 * 1024))))
    if len(refs) > maximum_files:
        for ref in refs:
            _blocked_asset(job, ref, "ASSET_FILE_COUNT_LIMIT")
        return {"found": len(refs), "ingested": 0, "existing": 0, "blocked": len(refs), "failed": 0}
    declared_total = sum(ref.size_bytes for ref in refs)
    if declared_total and declared_total > maximum_total:
        for ref in refs:
            _blocked_asset(job, ref, "ASSET_TOTAL_BYTES_LIMIT")
        return {"found": len(refs), "ingested": 0, "existing": 0, "blocked": len(refs), "failed": 0}
    maximum = max(1, int(os.getenv("AGENTGIGS_MAX_SOURCE_ASSET_BYTES", str(DEFAULT_MAX_SOURCE_BYTES))))
    upload_root = Path(os.getenv("AMARKTAI_UPLOAD_ROOT", "/var/lib/amarktai-earn/uploads")).resolve()
    target_root = upload_root / "agentgigs" / str(job.id)
    fetch = fetcher or _download_signed_asset
    ingested = existing = blocked = failed = 0
    current_total = JobAsset.objects.filter(job=job, status=JobAsset.Status.VERIFIED, duplicate_of=None).aggregate(total=Sum("size_bytes"))["total"] or 0

    for ref in refs:
        filename = safe_filename(ref.name, ref.external_id)
        try:
            role = _asset_role(job, ref, len(refs))
        except AssetPolicyError as exc:
            _blocked_asset(job, ref, exc.code)
            blocked += 1
            continue
        current = JobAsset.objects.filter(job=job, external_id=ref.external_id).first()
        if current and current.status == JobAsset.Status.VERIFIED and current.path:
            current_path = Path(current.path)
            if current_path.is_file() and (not ref.size_bytes or current_path.stat().st_size == ref.size_bytes):
                existing += 1
                continue
        if not supported_source_name(filename):
            _blocked_asset(job, ref, "UNSUPPORTED_SOURCE_FILE_TYPE")
            blocked += 1
            continue
        if ref.size_bytes and ref.size_bytes > maximum:
            _blocked_asset(job, ref, "SOURCE_FILE_TOO_LARGE")
            blocked += 1
            continue
        remaining = maximum_total - current_total
        if remaining <= 0 or (ref.size_bytes and ref.size_bytes > remaining):
            _blocked_asset(job, ref, "ASSET_TOTAL_BYTES_LIMIT")
            blocked += 1
            continue
        target = (target_root / f"{ref.external_id.rsplit(':', 1)[-1][:12]}-{filename}").resolve()
        try:
            if target_root.resolve() not in target.parents:
                raise AssetIngestionError("remote asset target escaped upload root")
            actual_size, content_type = fetch(ref, target, min(maximum, remaining))
            if ref.size_bytes and actual_size != ref.size_bytes:
                target.unlink(missing_ok=True)
                _blocked_asset(job, ref, "SOURCE_SIZE_MISMATCH")
                blocked += 1
                continue
            asset = stage_local_job_asset(
                job_id=job.id,
                path=str(target),
                source=f"agentgigs_{ref.source_kind}"[:40],
                external_id=ref.external_id,
                semantic_role=role,
                declared_mime_type=content_type,
            )
            AuditEvent.objects.create(
                event_type="job.asset_ingested",
                actor="agentgigs-asset-ingestor",
                metadata={
                    "job_id": str(job.id),
                    "asset_id": asset.id,
                    "source_kind": ref.source_kind,
                    "sha256": asset.sha256,
                    "size_bytes": asset.size_bytes,
                },
            )
            ingested += 1
            current_total += actual_size
        except (AgentGigsError, AssetIngestionError, RemoteAssetSafetyError, OSError, ValueError):
            target.unlink(missing_ok=True)
            _blocked_asset(job, ref, "SOURCE_DOWNLOAD_FAILED")
            failed += 1

    return {
        "found": len(refs),
        "ingested": ingested,
        "existing": existing,
        "blocked": blocked,
        "failed": failed,
    }


def sync_awarded_agentgigs_assets(adapter: AgentGigsAdapter, limit: int = 100) -> dict[str, int]:
    totals = {"jobs": 0, "found": 0, "ingested": 0, "existing": 0, "blocked": 0, "failed": 0}
    configured_cap = max(1, min(int(os.getenv("AGENTGIGS_MAX_ASSET_SYNC_JOBS_PER_CYCLE", "4")), 20))
    cycle_limit = max(1, min(int(limit), configured_cap))
    jobs = Job.objects.filter(
        marketplace__slug="agentgigs",
        state__in=[Job.State.CLAIMED, Job.State.AWARDED],
    ).order_by("updated_at")[:cycle_limit]
    for job in jobs:
        totals["jobs"] += 1
        try:
            result = ingest_agentgigs_assets(job, adapter)
        except (AgentGigsError, AssetIngestionError, RemoteAssetSafetyError, OSError, ValueError) as exc:
            totals["failed"] += 1
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="job.asset_sync_failed",
                actor="agentgigs-asset-ingestor",
                metadata={"job_id": str(job.id), "error_code": exc.__class__.__name__[:120]},
            )
            continue
        for key in ("found", "ingested", "existing", "blocked", "failed"):
            totals[key] += int(result.get(key, 0))
    return totals
