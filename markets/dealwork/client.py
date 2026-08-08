from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import requests

from markets.base import MarketAdapter, MarketCapabilities, NormalizedOpportunity
from markets.normalization import decimal_reward, first_value


class MCPClientError(RuntimeError):
    pass


class StreamableMCPClient:
    """Minimal MCP HTTP transport; tool schemas are discovered before any call."""

    def __init__(self, endpoint: str, api_key: str, timeout: int = 20, session=None):
        self.endpoint = endpoint
        self.timeout = timeout
        self.session = session or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._request_id = 0
        self._tools: set[str] | None = None

    def _rpc(self, method: str, params: dict | None = None) -> Any:
        self._request_id += 1
        body = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
        if params is not None:
            body["params"] = params
        try:
            response = self.session.post(self.endpoint, headers=self.headers, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise MCPClientError("Dealwork MCP request failed") from exc
        if not response.ok:
            raise MCPClientError(f"Dealwork MCP HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise MCPClientError("Dealwork MCP response was not JSON") from exc
        if payload.get("error"):
            raise MCPClientError("Dealwork MCP returned an RPC error")
        return payload.get("result", {})

    def list_tools(self) -> set[str]:
        result = self._rpc("tools/list")
        self._tools = {str(row.get("name")) for row in result.get("tools", []) if isinstance(row, dict)}
        return self._tools

    def call_tool(self, name: str, arguments: dict) -> Any:
        tools = self._tools if self._tools is not None else self.list_tools()
        if name not in tools:
            raise MCPClientError(f"Dealwork MCP tool is not advertised: {name}")
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        structured = result.get("structuredContent") if isinstance(result, dict) else None
        if structured is not None:
            return structured
        content = result.get("content", []) if isinstance(result, dict) else []
        for row in content:
            if isinstance(row, dict) and row.get("type") == "text":
                try:
                    return json.loads(row.get("text", ""))
                except (TypeError, ValueError):
                    return {"text": str(row.get("text", ""))}
        return result


def _rows(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("jobs", "items", "results", "data"):
            if isinstance(value.get(key), list):
                return [row for row in value[key] if isinstance(row, dict)]
    return []


class DealworkAdapter(MarketAdapter):
    slug = "dealwork"
    capabilities = MarketCapabilities(
        discover=True, claim=True, bid=True, messages=True, submission=True,
        revision=True, status=True, payment=True, payout=True,
        webhook_or_event_support=True, policy_verified=True, payout_ready=False,
    )

    def __init__(self, api_key: str, endpoint: str = "https://api.dealwork.ai/mcp", timeout: int = 20, mcp_client=None):
        if not api_key and mcp_client is None:
            raise ValueError("Dealwork API key is required")
        self.client = mcp_client or StreamableMCPClient(endpoint, api_key, timeout=timeout)

    def health(self):
        try:
            tools = self.client.list_tools()
            return {"ok": True, "tool_count": len(tools), "earning_tools": sorted(tools.intersection({"search_jobs", "claim_job", "place_bid", "submit_deliverable"}))}
        except MCPClientError as exc:
            return {"ok": False, "error": exc.__class__.__name__}

    def payout_status(self):
        return {
            "ready": False,
            "reason": "Withdrawal rail, KYA, account readiness, and South African non-crypto payout require external proof.",
        }

    def discover_jobs(self, **filters):
        args = {key: value for key, value in filters.items() if key in {"query", "category", "minBudget", "maxBudget", "limit"} and value not in (None, "")}
        return _rows(self.client.call_tool("search_jobs", args))

    def normalize_job(self, raw: dict) -> NormalizedOpportunity:
        reward = decimal_reward(raw, "budgetMax", "budget_max", "budget", "reward", "amount")
        return NormalizedOpportunity(
            external_id=str(first_value(raw, "id", "jobId", "job_id")),
            title=str(first_value(raw, "title", "name", default="Untitled Dealwork job")),
            task_class=str(first_value(raw, "category", "taskClass", "type", default="unknown")),
            reward=reward,
            currency=str(first_value(raw, "currency", default="USD"))[:3].upper(),
            raw=raw,
        )

    def claim(self, job):
        return self.client.call_tool("claim_job", {"jobId": job.external_id})

    def bid(self, job, amount):
        return self.client.call_tool("place_bid", {"jobId": job.external_id, "amount": str(Decimal(str(amount)))})

    def get_messages(self, job):
        contract_id = str(job.raw.get("contractId") or job.raw.get("contract_id") or "")
        if not contract_id:
            raise ValueError("Dealwork contract id is required for channel lookup")
        return self.client.call_tool("get_my_channels", {"contractId": contract_id})

    def submit(self, job, artifact):
        contract_id = str(job.raw.get("contractId") or job.raw.get("contract_id") or "")
        if not contract_id:
            raise ValueError("Dealwork contract id is required for deliverable submission")
        payload = {"contractId": contract_id, "deliverableUrl": str(artifact.get("url") or ""), "notes": str(artifact.get("notes") or "")}
        return self.client.call_tool("submit_deliverable", payload)

    def request_revision_event(self, job, event: str, details: dict | None = None):
        contract_id = str(job.raw.get("contractId") or job.raw.get("contract_id") or "")
        if not contract_id:
            raise ValueError("Dealwork contract id is required for contract events")
        return self.client.call_tool("send_contract_event", {"contractId": contract_id, "event": event, "details": details or {}})

    def get_status(self, job):
        contract_id = str(job.raw.get("contractId") or job.raw.get("contract_id") or "")
        if not contract_id:
            raise ValueError("Dealwork contract id is required for status reconciliation")
        return self.client.call_tool("get_contract", {"contractId": contract_id})

    def get_payout(self, job):
        return {"balance": self.client.call_tool("get_balance", {}), "ready": False, "settled": False}
