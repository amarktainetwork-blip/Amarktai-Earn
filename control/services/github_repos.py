from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from django.utils import timezone

from control.models import AuditEvent, Job
from planning.models import RepositorySnapshot


class GitHubRepositoryError(RuntimeError):
    pass


_GITHUB_URL = re.compile(r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_ALLOWED_ARCHIVE_HOSTS = {"codeload.github.com", "github.com", "objects.githubusercontent.com"}


@dataclass(frozen=True)
class GitHubRepoRef:
    owner: str
    repository: str
    url: str
    ref: str


def _payload(job: Job) -> dict[str, Any]:
    return job.normalized_payload if isinstance(job.normalized_payload, dict) else {}


def repository_ref(job: Job) -> GitHubRepoRef | None:
    raw = _payload(job)
    url = ""
    for key in ("repository_url", "repositoryUrl", "repo_url", "repoUrl", "github_url", "githubUrl"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            url = value.strip()
            break
    if not url:
        return None
    match = _GITHUB_URL.fullmatch(url)
    if not match:
        raise GitHubRepositoryError("only canonical https://github.com/OWNER/REPO repository URLs are supported")
    ref = ""
    for key in ("repository_ref", "repositoryRef", "git_ref", "gitRef", "branch", "base_branch", "baseBranch"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            ref = value.strip()[:255]
            break
    return GitHubRepoRef(match.group("owner"), match.group("repo"), url, ref)


def _headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": os.getenv("GITHUB_API_VERSION", "2022-11-28"),
        "User-Agent": "amarktai-earn/phase8c",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request_json(session, method: str, url: str, *, token: str, timeout: int) -> dict[str, Any]:
    response = session.request(method, url, headers=_headers(token), timeout=timeout)
    if not response.ok:
        raise GitHubRepositoryError(f"github API returned {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise GitHubRepositoryError("github API returned an invalid object")
    return payload


def _download_archive(session, ref: GitHubRepoRef, commit_sha: str, *, token: str, timeout: int, max_bytes: int) -> bytes:
    url = f"https://api.github.com/repos/{ref.owner}/{ref.repository}/tarball/{commit_sha}"
    first = session.request("GET", url, headers=_headers(token), timeout=timeout, allow_redirects=False)
    if first.status_code not in {301, 302, 303, 307, 308}:
        raise GitHubRepositoryError(f"github archive endpoint returned {first.status_code}")
    location = first.headers.get("Location", "")
    parsed = urlparse(location)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_ARCHIVE_HOSTS:
        raise GitHubRepositoryError("github archive redirect host is not trusted")
    response = session.request("GET", location, headers={"User-Agent": "amarktai-earn/phase8c"}, timeout=timeout, stream=True)
    if not response.ok:
        raise GitHubRepositoryError(f"github archive download returned {response.status_code}")
    data = bytearray()
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        if not chunk:
            continue
        data.extend(chunk)
        if len(data) > max_bytes:
            raise GitHubRepositoryError("repository archive exceeds configured compressed-size limit")
    return bytes(data)


def _safe_extract_tar(data: bytes, destination: Path, *, max_files: int, max_unpacked_bytes: int) -> tuple[int, int]:
    destination.mkdir(parents=True, exist_ok=True)
    file_count = 0
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        if not members:
            raise GitHubRepositoryError("repository archive is empty")
        top = members[0].name.split("/", 1)[0]
        for member in members:
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise GitHubRepositoryError("repository archive contains an unsafe special entry")
            parts = Path(member.name).parts
            if not parts or parts[0] != top:
                raise GitHubRepositoryError("repository archive has an unexpected root")
            relative = Path(*parts[1:]) if len(parts) > 1 else Path()
            if not relative.parts:
                continue
            if relative.is_absolute() or ".." in relative.parts:
                raise GitHubRepositoryError("repository archive attempted path traversal")
            target = (destination / relative).resolve()
            if destination.resolve() not in target.parents and target != destination.resolve():
                raise GitHubRepositoryError("repository archive escaped destination")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o700)
                continue
            if not member.isfile():
                raise GitHubRepositoryError("repository archive contains unsupported entry type")
            file_count += 1
            total_bytes += max(0, int(member.size))
            if file_count > max_files:
                raise GitHubRepositoryError("repository archive exceeds configured file-count limit")
            if total_bytes > max_unpacked_bytes:
                raise GitHubRepositoryError("repository archive exceeds configured unpacked-size limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise GitHubRepositoryError("repository archive file could not be read")
            with target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            # Preserve only the executable signal required by repository test
            # entrypoints; never carry group/world archive permissions through.
            target.chmod(0o700 if member.mode & 0o111 else 0o600)
    return file_count, total_bytes


def ensure_repository_snapshot(job_id, *, session=None) -> RepositorySnapshot | None:
    job = Job.objects.get(pk=job_id)
    ref = repository_ref(job)
    if ref is None:
        return None
    existing = RepositorySnapshot.objects.filter(job=job, status=RepositorySnapshot.Status.VERIFIED).first()
    if existing and existing.repository_url == ref.url and (not ref.ref or existing.ref == ref.ref) and existing.path and Path(existing.path).is_dir():
        return existing

    token = os.getenv("GITHUB_TOKEN", "").strip()
    timeout = max(5, min(int(os.getenv("GITHUB_REPOSITORY_TIMEOUT_SECONDS", "30")), 120))
    max_archive = max(1024 * 1024, min(int(os.getenv("GITHUB_REPOSITORY_MAX_ARCHIVE_BYTES", str(25 * 1024 * 1024))), 100 * 1024 * 1024))
    max_files = max(1, min(int(os.getenv("GITHUB_REPOSITORY_MAX_FILES", "5000")), 20000))
    max_unpacked = max(1024 * 1024, min(int(os.getenv("GITHUB_REPOSITORY_MAX_UNPACKED_BYTES", str(100 * 1024 * 1024))), 500 * 1024 * 1024))
    client = session or requests.Session()
    metadata = _request_json(client, "GET", f"https://api.github.com/repos/{ref.owner}/{ref.repository}", token=token, timeout=timeout)
    resolved_ref = ref.ref or str(metadata.get("default_branch") or "main")
    commit = _request_json(client, "GET", f"https://api.github.com/repos/{ref.owner}/{ref.repository}/commits/{resolved_ref}", token=token, timeout=timeout)
    commit_sha = str(commit.get("sha") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha):
        raise GitHubRepositoryError("github commit response did not contain a full SHA")
    data = _download_archive(client, ref, commit_sha, token=token, timeout=timeout, max_bytes=max_archive)

    root = Path(os.getenv("AMARKTAI_REPO_ROOT", "/var/lib/amarktai-earn/repos")).resolve()
    destination = (root / str(job.id) / commit_sha).resolve()
    if root not in destination.parents:
        raise GitHubRepositoryError("repository snapshot escaped configured repository root")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        file_count, total_bytes = _safe_extract_tar(data, destination, max_files=max_files, max_unpacked_bytes=max_unpacked)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise

    snapshot, _ = RepositorySnapshot.objects.update_or_create(
        job=job,
        defaults={
            "provider": "github",
            "repository_url": ref.url,
            "owner": ref.owner,
            "repository": ref.repository,
            "ref": resolved_ref,
            "commit_sha": commit_sha.lower(),
            "path": str(destination),
            "file_count": file_count,
            "total_bytes": total_bytes,
            "status": RepositorySnapshot.Status.VERIFIED,
            "error_code": "",
            "verified_at": timezone.now(),
        },
    )
    AuditEvent.objects.create(
        event_type="job.repository_snapshot_verified",
        actor="github-repository-gateway",
        metadata={"job_id": str(job.id), "repository": f"{ref.owner}/{ref.repository}", "ref": resolved_ref, "commit_sha": commit_sha.lower(), "files": file_count, "bytes": total_bytes},
    )
    return snapshot
