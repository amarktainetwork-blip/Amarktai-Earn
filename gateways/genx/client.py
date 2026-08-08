import time
import requests

class GenXError(RuntimeError): pass

class GenXClient:
    """Thin client for the documented GenX Router REST API. No model IDs are hard-coded."""
    def __init__(self, api_key: str, base_url: str = "https://query.genx.sh", timeout: int = 30):
        self.api_key, self.base_url, self.timeout = api_key, base_url.rstrip("/"), timeout
    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
    def _request(self, method, path, **kwargs):
        r = requests.request(method, self.base_url + path, headers=self.headers, timeout=self.timeout, **kwargs)
        if not r.ok:
            raise GenXError(f"GenX HTTP {r.status_code}")
        return r.json()
    def list_models(self, category: str | None = None):
        params = {"category": category} if category else None
        return self._request("GET", "/api/v1/models", params=params)
    def credits(self):
        return self._request("GET", "/api/v1/account/credits")
    def pricing(self, category: str | None = None):
        params = {"category": category} if category else None
        return self._request("GET", "/api/v1/account/pricing", params=params)
    def generate(self, model: str, params: dict, metadata: dict | None = None):
        payload = {"model": model, "params": params}
        if metadata: payload["metadata"] = {str(k): str(v) for k, v in list(metadata.items())[:16]}
        return self._request("POST", "/api/v1/generate", json=payload)
    def job(self, job_id: str):
        return self._request("GET", f"/api/v1/jobs/{job_id}")
    def wait(self, job_id: str, timeout_seconds: int = 180, poll_seconds: float = 2.0):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            data = self.job(job_id)
            status = data.get("status")
            if status in {"completed", "failed", "cancelled"}: return data
            time.sleep(poll_seconds)
        raise TimeoutError(f"GenX job {job_id} timed out")
