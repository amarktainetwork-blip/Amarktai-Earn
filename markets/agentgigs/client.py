from __future__ import annotations

import mimetypes
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from markets.base import MarketAdapter, MarketCapabilities, NormalizedOpportunity


class AgentGigsError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class AgentGigsAdapter(MarketAdapter):
    """AgentGigs REST adapter.

    AgentGigs exposes an API-first autonomous worker lifecycle, but the
    controller must keep acquisition disabled until payout readiness and South
    African Stripe Connect onboarding are verified in Amarktai's database.
    """

    slug = "agentgigs"
    capabilities = MarketCapabilities(
        discover=True,
        apply=True,
        messages=True,
        submit=True,
        payout=True,
        webhooks=True,
    )

    ALLOWED_DELIVERABLE_SUFFIXES = {
        ".pdf", ".zip", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".txt", ".csv", ".html", ".htm", ".md", ".markdown", ".png", ".jpg",
        ".jpeg", ".webp", ".gif", ".mp4", ".mp3", ".json",
    }
    # AgentGigs plan limits can be larger, but 50MB is the documented Pro limit
    # and is kept as a conservative controller-side ceiling for V1.
    MAX_DELIVERABLE_BYTES = 50 * 1024 * 1024

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://www.agentgigs.io",
        timeout: int = 20,
        session=None,
    ):
        if not api_key:
            raise ValueError("AgentGigs API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    @property
    def headers(self):
        return {"X-API-Key": self.api_key, "Accept": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = self.session.request(
                method,
                self.base_url + path,
                headers=self.headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            # Network ambiguity is intentionally not translated into a retryable
            # application result. The controller persists UNKNOWN_REMOTE_STATE.
            raise AgentGigsError(f"AgentGigs request failed: {exc.__class__.__name__}") from exc
        if not response.ok:
            retry_after = None
            if response.status_code == 429:
                try:
                    retry_after = int(response.headers.get("Retry-After", ""))
                except (TypeError, ValueError, AttributeError):
                    retry_after = None
            # Never include response body or credentials in errors/audit logs.
            raise AgentGigsError(
                f"AgentGigs HTTP {response.status_code}",
                status_code=response.status_code,
                retry_after=retry_after,
            )
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentGigsError("AgentGigs returned invalid JSON") from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    def health(self):
        try:
            data = self._request("GET", "/api/agent/jobs/available", params={"limit": 1})
            return {"ok": True, "count": data.get("count", len(data.get("jobs", [])))}
        except AgentGigsError as exc:
            return {"ok": False, "error": exc.__class__.__name__, "status_code": exc.status_code}

    def payout_status(self):
        # The public worker API cannot prove that this specific account's South
        # African Stripe Connect onboarding has completed. That gate is owned by
        # the controller and is never inferred from generic platform support.
        return {
            "ready": False,
            "requires_human_verification": True,
            "reason": "Stripe Connect onboarding and South African payout support must be verified before acquisition.",
        }

    def discover_jobs(self, **filters):
        allowed = {"category", "min_budget", "max_budget", "search", "limit"}
        params = {key: value for key, value in filters.items() if key in allowed and value not in (None, "")}
        if "limit" in params:
            params["limit"] = max(1, min(int(params["limit"]), 100))
        data = self._request("GET", "/api/agent/jobs/available", params=params or None)
        return data.get("jobs", [])

    def normalize_job(self, raw):
        budget = raw.get("budget_max") or raw.get("budget_min") or 0
        return NormalizedOpportunity(
            external_id=str(raw["id"]),
            title=raw.get("title", "Untitled"),
            task_class=raw.get("category", "unknown"),
            reward=Decimal(str(budget)) / Decimal("100"),
            raw=raw,
        )

    def nda_status(self, job_or_id) -> dict:
        external_id = getattr(job_or_id, "external_id", job_or_id)
        return self._request("GET", f"/api/jobs/{external_id}/nda")

    def ensure_nda(self, job_or_id) -> dict:
        external_id = getattr(job_or_id, "external_id", job_or_id)
        status = self.nda_status(external_id)
        if status.get("accepted"):
            return status
        return self._request("POST", f"/api/jobs/{external_id}/nda")

    def apply(self, job, amount, message):
        """Apply once after confidentiality acknowledgement.

        Current AgentGigs documentation describes /api/jobs/{id}/apply as the
        regular/proofer application endpoint, while an older endpoint listing
        still shows /api/agent/jobs/{id}/accept. Prefer /apply and only fall back
        on a definitive 404/405; never replay after a timeout/5xx ambiguity.
        """
        self.ensure_nda(job)
        cents = int((Decimal(str(amount)) * 100).quantize(Decimal("1")))
        payload = {
            "proposed_price": cents,
            "message": message,
            "estimated_delivery": "24 hours",
        }
        try:
            return self._request("POST", f"/api/jobs/{job.external_id}/apply", json=payload)
        except AgentGigsError as exc:
            if exc.status_code not in {404, 405}:
                raise
            return self._request("POST", f"/api/agent/jobs/{job.external_id}/accept", json=payload)

    def applications(self, status: str | None = None, limit: int = 50) -> dict:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        if status:
            params["status"] = status
        return self._request("GET", "/api/agent/applications", params=params)

    def get_status(self, job):
        return self._request("GET", f"/api/agent/jobs/{job.external_id}/details")

    def get_messages(self, job):
        self.ensure_nda(job)
        data = self._request("GET", f"/api/jobs/{job.external_id}/messages")
        return data.get("messages", [])

    def send_message(self, job, message: str):
        self.ensure_nda(job)
        return self._request("POST", f"/api/jobs/{job.external_id}/messages", json={"message": message})

    def notifications(self, unread: bool = False, limit: int = 50):
        return self._request(
            "GET",
            "/api/agent/notifications",
            params={"unread": str(bool(unread)).lower(), "limit": max(1, min(int(limit), 100))},
        )

    def earnings(self, calculate_cents: int | None = None):
        params = {"calculate": int(calculate_cents)} if calculate_cents is not None else None
        return self._request("GET", "/api/agent/earnings", params=params)

    def register_webhook(self, url: str, events: list[str] | None = None):
        return self._request(
            "POST",
            "/api/agent/webhooks",
            json={
                "url": url,
                "events": events
                or ["job.available", "job.accepted", "job.revision_requested", "job.approved", "payment.released"],
            },
        )

    def upload_deliverable(self, job, local_path: str | Path):
        self.ensure_nda(job)
        path = Path(local_path).resolve()
        if not path.is_file():
            raise AgentGigsError("deliverable file does not exist")
        if path.suffix.lower() not in self.ALLOWED_DELIVERABLE_SUFFIXES:
            raise AgentGigsError("deliverable file type is not allowed by AgentGigs")
        if path.stat().st_size > self.MAX_DELIVERABLE_BYTES:
            raise AgentGigsError("deliverable exceeds the conservative 50MB upload limit")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as handle:
            return self._request(
                "POST",
                f"/api/agent/jobs/{job.external_id}/upload-deliverable",
                files={"file": (path.name, handle, mime)},
            )

    @staticmethod
    def _uploaded_url(payload: dict[str, Any]) -> str:
        for key in ("url", "deliverable_url", "file_url", "download_url", "attachment_url"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        file_row = payload.get("file")
        if isinstance(file_row, dict):
            for key in ("url", "deliverable_url", "download_url"):
                value = file_row.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    @staticmethod
    def _url_from_details(details: dict[str, Any], filename: str = "") -> str:
        job = details.get("job")
        if isinstance(job, dict):
            value = job.get("deliverable_url")
            if isinstance(value, str) and value:
                return value
        files = details.get("deliverable_files")
        if isinstance(files, list):
            candidates = [row for row in files if isinstance(row, dict)]
            if filename:
                named = [row for row in candidates if str(row.get("file_name") or "") == filename]
                if named:
                    candidates = named
            for row in reversed(candidates):
                value = row.get("download_url")
                if isinstance(value, str) and value:
                    return value
        return ""

    def submit(self, job, artifact):
        self.ensure_nda(job)
        deliverable_url = str(artifact.get("url") or "")
        local_path = artifact.get("path")
        upload_response = None
        filename = ""
        if local_path:
            filename = Path(local_path).name
            upload_response = self.upload_deliverable(job, local_path)
            deliverable_url = self._uploaded_url(upload_response) or deliverable_url
            if not deliverable_url:
                # Secure upload responses are not fully documented. Reconcile the
                # newly associated platform file via job details before submit.
                deliverable_url = self._url_from_details(self.get_status(job), filename=filename)
        if not deliverable_url:
            raise AgentGigsError("no deliverable URL was available after upload; reconcile job details before retrying")
        response = self._request(
            "POST",
            f"/api/agent/jobs/{job.external_id}/submit",
            json={
                "deliverable_url": deliverable_url,
                "notes": artifact.get("notes", "Completed and independently QA verified."),
            },
        )
        if upload_response is not None:
            response = {**response, "upload": upload_response, "deliverable_url": deliverable_url}
        return response

    def get_payout(self, job):
        # Account-level earnings are useful for calculator/account reconciliation,
        # but payment.released is the authoritative per-job settlement event.
        return {"job": self.get_status(job), "earnings": self.earnings()}
