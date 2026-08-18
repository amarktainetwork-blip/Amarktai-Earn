from __future__ import annotations

from decimal import Decimal

from markets.base import MarketAdapter, MarketCapabilities, NormalizedOpportunity
from markets.http import JSONHTTPClient, MarketHTTPError
from markets.normalization import decimal_reward, first_value, list_rows


class TaskBountyAdapter(MarketAdapter):
    slug = "taskbounty"
    capabilities = MarketCapabilities(
        discover=True, claim=True, input_assets=True, repo_access=True, submission=True,
        delivery=True, status=True, payment=True, payout=True, settlement_status=True,
        rate_limit=True, policy_verified=True, payout_ready=False,
    )

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
            "crypto_prohibited": True,
            "supported_method": "usd_bank_transfer",
            "selected_method": "usd_bank_transfer",
            "reason": (
                "USD bank transfer onboarding is an owner dashboard action; no payout method is configured by this adapter."
            ),
        }

    def set_payout_method(self, method: str, address: str):
        raise ValueError("TaskBounty payout API is disabled: AmarktAI uses owner-configured USD bank transfer only")

    def discover_jobs(self, **filters):
        params = {"state": filters.get("state", "open"), "limit": max(1, min(int(filters.get("limit", 50)), 100))}
        return list_rows(self.http.request("GET", "/tasks", params=params), "tasks", "items")

    def normalize_job(self, raw: dict) -> NormalizedOpportunity:
        reward = decimal_reward(raw, "reward", "bounty", "amount", "payout")
        if reward == 0:
            reward = decimal_reward(raw, "reward_cents", "amount_cents", cents=True)
        category = str(first_value(raw, "type", "category", default="bug")).casefold()
        action = "TASKBOUNTY_COVERAGE" if "coverage" in category else "TASKBOUNTY_BUG_FIX"
        return NormalizedOpportunity(
            external_id=str(first_value(raw, "id", "task_id")),
            title=str(first_value(raw, "title", "issue_title", default="Untitled TaskBounty task")),
            task_class=str(first_value(raw, "language", "category", "type", default="coding")),
            reward=reward,
            currency=str(first_value(raw, "currency", default="USD"))[:3].upper(),
            raw=raw,
            action=action,
            fee_rate=Decimal("0.20"),
            payout_probability=Decimal(str(first_value(raw, "payout_probability", default="0.90"))),
            acceptance_probability=Decimal(str(first_value(raw, "acceptance_probability", default="0.65"))),
            expected_provider_cost=Decimal(str(first_value(raw, "expected_provider_cost", default="0"))),
            expected_execution_cost=Decimal(str(first_value(raw, "expected_execution_cost", default="0"))),
            expected_minutes=int(first_value(raw, "expected_minutes", default=60)),
            competition={
                "solvers": int(first_value(raw, "solver_count", "solvers", default=0) or 0),
                "existing_prs": int(first_value(raw, "existing_prs", "pr_count", default=0) or 0),
                "claim_exclusive": bool(first_value(raw, "claim_exclusive", default=False)),
            },
            capabilities_required=("coding", "git", "tests", "sandbox"),
        )

    def claim(self, job):
        if self.mcp_client is None:
            raise NotImplementedError("Configure the official TaskBounty MCP client for claim; a REST path is not guessed")
        claim = getattr(self.mcp_client, "claim", None)
        if not callable(claim):
            raise NotImplementedError("The configured TaskBounty MCP binding must expose its verified claim operation")
        return claim(task_id=job.external_id)

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
            "rail": "USD_BANK_TRANSFER",
            "reason": "A verified TaskBounty bank transfer receipt must be reconciled before revenue is settled.",
        }
