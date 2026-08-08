from __future__ import annotations

import time
from typing import Any
import requests


class GenXError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GenXClient:
    """Thin client for the documented GenX Router REST API. No model IDs are hard-coded."""

    def __init__(self, api_key: str, base_url: str = "https://query.genx.sh", timeout: int = 30, session=None):
        if not api_key:
            raise ValueError("GenX API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    @property
    def auth_headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    @property
    def headers(self):
        return {**self.auth_headers, "Content-Type": "application/json"}

    def _request(self, method: str, path: str, *, retries: int = 2, **kwargs) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = self.session.request(
                    method,
                    self.base_url + path,
                    headers=self.headers,
                    timeout=self.timeout,
                    **kwargs,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < retries:
                        time.sleep(min(2 ** attempt, 4))
                        continue
                if not response.ok:
                    raise GenXError(f"GenX HTTP {response.status_code}", status_code=response.status_code)
                if not response.content:
                    return {}
                data = response.json()
                return data if isinstance(data, dict) else {"data": data}
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                if isinstance(exc, GenXError):
                    raise
                raise GenXError(f"GenX request failed: {exc.__class__.__name__}") from exc
        raise GenXError(f"GenX request failed: {last_error.__class__.__name__ if last_error else 'unknown'}")

    def list_models(self, category: str | None = None):
        params = {"category": category} if category else None
        return self._request("GET", "/api/v1/models", params=params)

    def model(self, model_id: str):
        return self._request("GET", f"/api/v1/models/{model_id}")

    def credits(self):
        return self._request("GET", "/api/v1/account/credits")

    def pricing(self, category: str | None = None):
        params = {"category": category} if category else None
        return self._request("GET", "/api/v1/account/pricing", params=params)

    def model_pricing(self, model_id: str):
        return self._request("GET", f"/api/v1/account/pricing/{model_id}")

    def generate(self, model: str, params: dict, metadata: dict | None = None):
        payload = {"model": model, "params": params}
        if metadata:
            payload["metadata"] = {str(k): str(v) for k, v in list(metadata.items())[:16]}
        return self._request("POST", "/api/v1/generate", json=payload, retries=0)

    def job(self, job_id: str):
        return self._request("GET", f"/api/v1/jobs/{job_id}")

    def result(self, job_id: str):
        return self._request("GET", f"/api/v1/jobs/{job_id}/result")

    def upload_file(self, path):
        from pathlib import Path

        source = Path(path)
        if not source.is_file():
            raise GenXError("GenX upload source file does not exist")
        if source.stat().st_size > 100 * 1024 * 1024:
            raise GenXError("GenX upload exceeds documented 100MB file limit")
        try:
            with source.open("rb") as handle:
                response = self.session.request(
                    "POST",
                    self.base_url + "/api/v1/files",
                    headers=self.auth_headers,
                    timeout=self.timeout,
                    files={"file": (source.name, handle, "application/octet-stream")},
                )
        except requests.RequestException as exc:
            raise GenXError(f"GenX file upload failed: {exc.__class__.__name__}") from exc
        if not response.ok:
            raise GenXError(f"GenX HTTP {response.status_code}", status_code=response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise GenXError("GenX file upload returned non-JSON metadata") from exc
        return data if isinstance(data, dict) else {"data": data}

    def delete_file(self, file_id: str):
        if not file_id:
            raise GenXError("GenX file ID is required")
        return self._request("DELETE", f"/api/v1/files/{file_id}", retries=0)

    def create_session(self, model: str, *, system_prompt: str = "", title: str = ""):
        payload = {"model": model}
        if system_prompt:
            payload["system_prompt"] = system_prompt
        if title:
            payload["title"] = title
        return self._request("POST", "/api/v1/sessions", json=payload, retries=0)

    def session_message(
        self,
        session_id: str,
        message: str,
        *,
        idempotency_key: str = "",
        tools: list | None = None,
        file_ids: list[str] | None = None,
    ):
        payload = {"message": message}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        if tools:
            payload["tools"] = tools
        if file_ids:
            payload["files"] = file_ids
        return self._request("POST", f"/api/v1/sessions/{session_id}/messages", json=payload, retries=0)

    def session_messages(self, session_id: str):
        return self._request("GET", f"/api/v1/sessions/{session_id}/messages")

    def close_session(self, session_id: str):
        return self._request("POST", f"/api/v1/sessions/{session_id}/close", retries=0)

    def job_file(self, job_id: str, *, max_bytes: int = 20 * 1024 * 1024) -> bytes:
        try:
            response = self.session.request(
                "GET",
                self.base_url + f"/api/v1/jobs/{job_id}/file",
                headers=self.auth_headers,
                timeout=self.timeout,
                stream=True,
            )
        except requests.RequestException as exc:
            raise GenXError(f"GenX result download failed: {exc.__class__.__name__}") from exc
        if not response.ok:
            raise GenXError(f"GenX HTTP {response.status_code}", status_code=response.status_code)
        chunks = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise GenXError("GenX result file exceeds configured limit")
                chunks.append(chunk)
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        return b"".join(chunks)

    def cancel(self, job_id: str):
        return self._request("POST", f"/api/v1/jobs/{job_id}/cancel", retries=0)

    def wait(self, job_id: str, timeout_seconds: int = 180, poll_seconds: float = 2.0):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            data = self.job(job_id)
            status = str(data.get("status", "")).lower()
            if status in {"completed", "failed", "cancelled"}:
                return data
            time.sleep(poll_seconds)
        raise TimeoutError(f"GenX job {job_id} timed out")
