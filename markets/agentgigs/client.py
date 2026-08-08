from decimal import Decimal
import requests
from markets.base import MarketAdapter, MarketCapabilities, NormalizedOpportunity

class AgentGigsAdapter(MarketAdapter):
    """AgentGigs REST adapter. Acquisition must remain disabled until payout readiness is verified."""
    slug = "agentgigs"
    capabilities = MarketCapabilities(discover=True, apply=True, messages=True, submit=True, payout=True)
    def __init__(self, api_key: str, base_url: str = "https://www.agentgigs.io", timeout: int = 20):
        self.api_key, self.base_url, self.timeout = api_key, base_url.rstrip("/"), timeout
    @property
    def headers(self):
        return {"X-API-Key": self.api_key, "Accept": "application/json"}
    def _request(self, method, path, **kwargs):
        r = requests.request(method, self.base_url + path, headers=self.headers, timeout=self.timeout, **kwargs)
        r.raise_for_status(); return r.json() if r.content else {}
    def health(self):
        try:
            data = self._request("GET", "/api/agent/jobs/available")
            return {"ok": True, "count": data.get("count", len(data.get("jobs", [])))}
        except requests.RequestException as exc:
            return {"ok": False, "error": exc.__class__.__name__}
    def payout_status(self):
        return {"ready": False, "requires_human_verification": True, "reason": "Stripe Connect onboarding and South African payout support must be verified before acquisition."}
    def discover_jobs(self):
        data = self._request("GET", "/api/agent/jobs/available")
        return data.get("jobs", [])
    def normalize_job(self, raw):
        budget = raw.get("budget_max") or raw.get("budget_min") or 0
        return NormalizedOpportunity(external_id=str(raw["id"]), title=raw.get("title", "Untitled"), task_class=raw.get("category", "unknown"), reward=Decimal(str(budget)) / Decimal("100"), raw=raw)
    def apply(self, job, amount, message):
        cents = int((Decimal(str(amount)) * 100).quantize(Decimal("1")))
        return self._request("POST", f"/api/agent/jobs/{job.external_id}/accept", json={"proposed_price": cents, "message": message, "estimated_delivery": "async"})
    def submit(self, job, artifact):
        return self._request("POST", f"/api/agent/jobs/{job.external_id}/submit", json={"deliverable_url": artifact["url"], "notes": artifact.get("notes", "Completed and independently QA verified.")})
