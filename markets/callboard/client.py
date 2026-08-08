from __future__ import annotations

from markets.base import MarketAdapter, MarketCapabilities, NormalizedOpportunity
from markets.http import JSONHTTPClient, MarketHTTPError
from markets.normalization import decimal_reward, first_value, list_rows


class CallboardAdapter(MarketAdapter):
    slug = "callboard"
    capabilities = MarketCapabilities(
        discover=True, apply=True, input_assets=True, submission=True, status=True,
        payment=True, payout=True, webhook_or_event_support=True, rate_limit=True,
        policy_verified=True, payout_ready=False,
    )

    def __init__(self, api_key: str, base_url: str = "https://getcallboard.com", timeout: int = 20, session=None):
        if not api_key:
            raise ValueError("Callboard API key is required")
        self.http = JSONHTTPClient(
            market="Callboard", base_url=base_url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout, session=session,
        )

    def health(self) -> dict:
        try:
            return {"ok": True, "agent": self.http.request("GET", "/api/v2/agents/me")}
        except MarketHTTPError as exc:
            return {"ok": False, "status_code": exc.status_code, "error": exc.__class__.__name__}

    def payout_status(self) -> dict:
        return {
            "ready": False,
            "reason": "Account-specific Stripe Connect payouts and South African eligibility require external proof.",
        }

    def discover_jobs(self, **filters) -> list[dict]:
        allowed = {"capability", "limit", "include"}
        params = {key: value for key, value in filters.items() if key in allowed and value not in (None, "")}
        if "limit" in params:
            params["limit"] = max(1, min(int(params["limit"]), 100))
        query = str(filters.get("search") or filters.get("q") or "").strip()
        path = "/api/v2/jobs/search" if query else "/api/v2/jobs"
        if query:
            params["q"] = query
        return list_rows(self.http.request("GET", path, params=params or None), "jobs", "items")

    def normalize_job(self, raw: dict) -> NormalizedOpportunity:
        reward = decimal_reward(raw, "pay", "reward", "budget", "amount")
        if reward == 0:
            reward = decimal_reward(raw, "payCents", "rewardCents", "budgetCents", cents=True)
        return NormalizedOpportunity(
            external_id=str(first_value(raw, "id", "jobId")),
            title=str(first_value(raw, "title", "name", default="Untitled Callboard job")),
            task_class=str(first_value(raw, "jobTypeKey", "capability", "category", default="unknown")),
            reward=reward,
            currency=str(first_value(raw, "currency", default="USD"))[:3].upper(),
            raw=raw,
        )

    def apply(self, job, amount, message):
        # The API-key application endpoint is explicitly documented with no body.
        return self.http.request("POST", f"/api/v2/jobs/{job.external_id}/applications")

    def readiness(self):
        return self.http.request("GET", "/api/v2/home")

    def heartbeat(self, payload: dict):
        return self.http.request("POST", "/api/v2/agents/me/heartbeat", json=payload)

    def applications(self):
        return self.http.request("GET", "/api/v2/worker-agents/me/applications")

    def participation_slots(self):
        return self.http.request("GET", "/api/v2/worker-agents/me/participation-slots")

    def acknowledge(self, slot_id: str):
        return self.http.request("POST", f"/api/v2/participation-slots/{slot_id}/acknowledge")

    def get_input_assets(self, slot_id: str):
        return list_rows(
            self.http.request("GET", f"/api/v2/participation-slots/{slot_id}/input-files"),
            "files", "inputFiles", "items",
        )

    def submit(self, job, artifact):
        slot_id = str(artifact.get("participation_slot_id") or job.raw.get("participation_slot_id") or "")
        market_payload = artifact.get("market_payload")
        if not slot_id:
            raise ValueError("Callboard participation_slot_id is required for submission")
        if not isinstance(market_payload, dict):
            raise ValueError("Callboard OpenAPI-shaped market_payload is required; submission fields are not guessed")
        return self.http.request("POST", f"/api/v2/participation-slots/{slot_id}/submit", json=market_payload)

    def get_status(self, job):
        return self.http.request("GET", f"/api/v2/jobs/{job.external_id}")

    def submission_status(self, submission_id: str):
        return self.http.request("GET", f"/api/v2/submissions/{submission_id}/status")

    def get_payout(self, job):
        return self.http.request("GET", f"/api/v2/jobs/{job.external_id}/payment")
