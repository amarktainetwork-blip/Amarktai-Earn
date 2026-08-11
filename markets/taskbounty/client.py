from __future__ import annotations

from markets.base import MarketAdapter, MarketCapabilities, NormalizedOpportunity
from markets.http import JSONHTTPClient, MarketHTTPError
from markets.normalization import decimal_reward, first_value, list_rows


class TaskBountyAdapter(MarketAdapter):
    slug = "taskbounty"
    capabilities = MarketCapabilities(
        discover=True, input_assets=True, submission=True, status=True, payment=True,
        payout=True, rate_limit=True, policy_verified=True, payout_ready=False,
    )

    PAYOUT_METHODS = frozenset({"solana_usdc", "eth", "btc"})

    def __init__(self, api_key: str, base_url: str = "https://www.task-bounty.com/api/v1", timeout: int = 20, session=None, mcp_client=None):
        if not api_key:
            raise ValueError("TaskBounty API key is required")
        self.http = JSONHTTPClient(
            market="TaskBounty", base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout, session=session,
        )
        self.mcp_client = mcp_client

    def health(self):
        try:
            payload = self.http.request("GET", "/tasks", params={"state": "open", "limit": 1})
            return {"ok": True, "count": len(list_rows(payload, "tasks", "items"))}
        except MarketHTTPError as exc:
            return {"ok": False, "status_code": exc.status_code, "error": exc.__class__.__name__}

    def payout_status(self):
        return {
            "ready": False,
            "crypto_prohibited": False,
            "supported_external_methods": sorted(self.PAYOUT_METHODS),
            "reason": (
                "TaskBounty supports public-address crypto payout registration. "
                "AmarktAI treats the route as ready only after the owner records non-secret proof of the configured external address."
            ),
        }

    def set_payout_method(self, method: str, address: str):
        method = str(method or "").strip().lower()
        address = str(address or "").strip()
        if method not in self.PAYOUT_METHODS:
            raise ValueError("Unsupported TaskBounty payout method")
        if not address:
            raise ValueError("TaskBounty payout address is required")
        # TaskBounty's solver API accepts public payout addresses only. This
        # adapter must never accept or transmit private keys, seed phrases or
        # signing credentials.
        return self.http.request(
            "POST",
            "/solver/payout-method",
            json={"method": method, "address": address},
        )

    def discover_jobs(self, **filters):
        params = {"state": filters.get("state", "open"), "limit": max(1, min(int(filters.get("limit", 50)), 100))}
        return list_rows(self.http.request("GET", "/tasks", params=params), "tasks", "items")

    def normalize_job(self, raw: dict) -> NormalizedOpportunity:
        reward = decimal_reward(raw, "reward", "bounty", "amount", "payout")
        if reward == 0:
            reward = decimal_reward(raw, "reward_cents", "amount_cents", cents=True)
        return NormalizedOpportunity(
            external_id=str(first_value(raw, "id", "task_id")),
            title=str(first_value(raw, "title", "issue_title", default="Untitled TaskBounty task")),
            task_class=str(first_value(raw, "language", "category", "type", default="coding")),
            reward=reward,
            currency=str(first_value(raw, "currency", default="USD"))[:3].upper(),
            raw=raw,
        )

    def get_input_assets(self, job):
        return self.http.request("POST", f"/tasks/{job.external_id}/access")

    def submit(self, job, artifact):
        external_link = str(artifact.get("external_link") or artifact.get("url") or "")
        if not external_link.startswith("https://github.com/"):
            raise ValueError("TaskBounty submission requires an upstream GitHub PR URL")
        return self.http.request(
            "POST", "/submissions", json={"task_id": job.external_id, "external_link": external_link}
        )

    def get_status(self, job):
        submission_id = str(job.raw.get("submission_id") or "")
        if not submission_id:
            raise ValueError("TaskBounty submission_id is required for status reconciliation")
        if self.mcp_client is None:
            raise NotImplementedError("Configure the official MCP client for check_submission_status; REST path is not guessed")
        return self.mcp_client.call_tool("check_submission_status", {"submission_id": submission_id})

    def get_payout(self, job):
        return {
            "ready": False,
            "settled": False,
            "reason": "External payout receipt must be reconciled from TaskBounty and the configured public-address receipt evidence.",
        }
