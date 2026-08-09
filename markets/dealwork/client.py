from __future__ import annotations

from decimal import Decimal
from typing import Any

import requests

from markets.base import MarketAdapter, MarketCapabilities, NormalizedOpportunity
from markets.normalization import decimal_reward, first_value


class DealworkAPIError(RuntimeError):
    pass


def _rows(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("jobs", "items", "results", "data"):
            if isinstance(value.get(key), list):
                return [row for row in value[key] if isinstance(row, dict)]
    return []


def _data(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value


class DealworkRESTClient:
    """Official Dealwork REST transport using the agent Bearer credential."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://dealwork.ai",
        timeout: int = 20,
        session=None,
    ):
        if not api_key:
            raise ValueError("Dealwork API key is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    def request(self, method: str, path: str, *, params=None, json=None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                headers=self.headers,
                params=params,
                json=json,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise DealworkAPIError("Dealwork REST request failed") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise DealworkAPIError(
                f"Dealwork REST HTTP {response.status_code} returned non-JSON"
            ) from exc

        if not response.ok:
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                code = str(error.get("code") or "").strip()
                message = str(error.get("message") or "").strip()
            else:
                code = ""
                message = ""
            detail = " ".join(part for part in (code, message) if part)
            suffix = f" {detail[:240]}" if detail else ""
            raise DealworkAPIError(
                f"Dealwork REST HTTP {response.status_code}{suffix}"
            )

        return payload

    def get(self, path: str, *, params=None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, *, json=None) -> Any:
        return self.request("POST", path, json=json or {})


class DealworkAdapter(MarketAdapter):
    slug = "dealwork"

    capabilities = MarketCapabilities(
        discover=True,
        claim=True,
        bid=True,
        messages=True,
        submission=True,
        revision=True,
        status=True,
        payment=True,
        payout=True,
        webhook_or_event_support=True,
        rate_limit=True,
        policy_verified=True,
        payout_ready=False,
    )

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://dealwork.ai",
        timeout: int = 20,
        session=None,
    ):
        self.client = DealworkRESTClient(
            api_key,
            base_url=base_url,
            timeout=timeout,
            session=session,
        )

    def health(self):
        try:
            payload = self.client.get(
                "/api/v1/jobs",
                params={"per_page": 1, "sort": "newest"},
            )
            return {
                "ok": True,
                "transport": "REST",
                "jobs_visible": len(_rows(payload)),
                "earning_operations": [
                    "discover",
                    "bid",
                    "claim",
                    "submit",
                    "messages",
                    "status",
                    "wallet",
                ],
            }
        except DealworkAPIError as exc:
            return {
                "ok": False,
                "transport": "REST",
                "error": exc.__class__.__name__,
            }

    def payout_status(self):
        return {
            "ready": False,
            "reason": (
                "Dealwork wallet is proven readable, but external withdrawal "
                "rail and final settlement remain separately unverified."
            ),
        }

    def discover_jobs(self, **filters):
        limit = max(1, min(int(filters.get("limit") or 20), 50))

        params = {
            "per_page": limit,
            "page": max(1, int(filters.get("page") or 1)),
            "sort": str(filters.get("sort") or "newest"),
        }

        aliases = {
            "category": "category",
            "eligible_worker_types": "eligible_worker_types",
            "eligibleWorkerTypes": "eligible_worker_types",
            "minBudget": "budget_min",
            "budget_min": "budget_min",
            "maxBudget": "budget_max",
            "budget_max": "budget_max",
        }

        for incoming, outgoing in aliases.items():
            value = filters.get(incoming)
            if value not in (None, ""):
                params[outgoing] = value

        rows = _rows(self.client.get("/api/v1/jobs", params=params))

        query = str(filters.get("query") or "").strip().casefold()
        if query:
            rows = [
                row
                for row in rows
                if query
                in (
                    f"{row.get('title', '')} "
                    f"{row.get('description', '')} "
                    f"{row.get('category', '')}"
                ).casefold()
            ]

        return rows

    def normalize_job(self, raw: dict) -> NormalizedOpportunity:
        reward = decimal_reward(
            raw,
            "fixedPrice",
            "fixed_price",
            "budgetMax",
            "budget_max",
            "budget",
            "reward",
            "amount",
        )
        return NormalizedOpportunity(
            external_id=str(first_value(raw, "id", "jobId", "job_id")),
            title=str(
                first_value(
                    raw,
                    "title",
                    "name",
                    default="Untitled Dealwork job",
                )
            ),
            task_class=str(
                first_value(
                    raw,
                    "category",
                    "taskClass",
                    "type",
                    default="unknown",
                )
            ),
            reward=reward,
            currency=str(
                first_value(raw, "currency", default="USD")
            )[:3].upper(),
            raw=raw,
        )

    def claim(self, job):
        criteria = job.raw.get("acceptanceCriteria") or []
        accepted_ids = [
            str(row.get("id"))
            for row in criteria
            if isinstance(row, dict) and row.get("id")
        ]

        payload = {
            "acceptedCriteriaIds": accepted_ids,
        }

        return _data(
            self.client.post(
                f"/api/v1/jobs/{job.external_id}/claim",
                json=payload,
            )
        )

    def _proposal_text(self, job) -> str:
        supplied = str(
            job.raw.get("proposalText")
            or job.raw.get("proposal_text")
            or ""
        ).strip()

        if supplied:
            return supplied[:5000]

        description = str(job.raw.get("description") or "").strip()
        description = " ".join(description.split())

        if description:
            requirement = description[:450]
            return (
                f"For '{job.title}', I will follow the posted requirements: "
                f"{requirement}. I will validate the completed deliverable "
                "against the listed acceptance criteria before submission."
            )[:5000]

        return (
            f"For '{job.title}', I will complete the requested "
            f"{job.task_class} work and validate the result against the "
            "job's acceptance criteria before submission."
        )[:5000]

    def _estimated_hours(self, job) -> float:
        explicit = (
            job.raw.get("estimatedHours")
            or job.raw.get("estimated_hours")
        )
        if explicit not in (None, ""):
            try:
                parsed = float(explicit)
                if parsed > 0:
                    return parsed
            except (TypeError, ValueError):
                pass

        reward = Decimal(str(job.reward))
        if reward <= Decimal("15"):
            return 1.0
        if reward <= Decimal("50"):
            return 2.0
        return 4.0

    def bid(self, job, amount):
        proposed = Decimal(str(amount)).quantize(Decimal("0.01"))

        payload = {
            "proposedAmount": str(proposed),
            "estimatedHours": self._estimated_hours(job),
            "proposalText": self._proposal_text(job),
        }

        return _data(
            self.client.post(
                f"/api/v1/jobs/{job.external_id}/bids",
                json=payload,
            )
        )

    @staticmethod
    def _contract_id(job) -> str:
        return str(
            job.raw.get("contractId")
            or job.raw.get("contract_id")
            or ""
        ).strip()

    def get_messages(self, job):
        contract_id = self._contract_id(job)
        if not contract_id:
            raise ValueError(
                "Dealwork contract id is required for messages"
            )

        return _data(
            self.client.get(
                f"/api/v1/contracts/{contract_id}/messages"
            )
        )

    def submit(self, job, artifact):
        contract_id = self._contract_id(job)
        if not contract_id:
            raise ValueError(
                "Dealwork contract id is required for deliverable submission"
            )

        description = str(
            artifact.get("description")
            or artifact.get("notes")
            or "Completed task"
        ).strip()[:5000]

        output_data = artifact.get("outputData")
        if not isinstance(output_data, dict):
            output_data = artifact.get("market_payload")

        if not isinstance(output_data, dict):
            output_data = {}

            url = str(artifact.get("url") or "").strip()
            if url:
                output_data["artifactUrl"] = url

            files = artifact.get("files")
            if isinstance(files, dict):
                output_data["files"] = files

            notes = str(artifact.get("notes") or "").strip()
            if notes:
                output_data["notes"] = notes

        if not output_data:
            raise ValueError(
                "Dealwork deliverable output data is required"
            )

        deliverable = _data(
            self.client.post(
                f"/api/v1/contracts/{contract_id}/deliverables",
                json={
                    "description": description,
                    "outputData": output_data,
                },
            )
        )

        deliverable_id = (
            deliverable.get("id")
            if isinstance(deliverable, dict)
            else None
        )

        if not deliverable_id:
            raise DealworkAPIError(
                "Dealwork deliverable response omitted id"
            )

        submission = _data(
            self.client.post(
                f"/api/v1/contracts/{contract_id}/events",
                json={
                    "type": "SUBMIT_WORK",
                    "deliverableId": str(deliverable_id),
                },
            )
        )

        return {
            "deliverable": deliverable,
            "submission": submission,
        }

    def request_revision_event(
        self,
        job,
        event: str,
        details: dict | None = None,
    ):
        contract_id = self._contract_id(job)
        if not contract_id:
            raise ValueError(
                "Dealwork contract id is required for contract events"
            )

        payload = {"type": str(event)}
        if details:
            payload.update(details)

        return _data(
            self.client.post(
                f"/api/v1/contracts/{contract_id}/events",
                json=payload,
            )
        )

    def get_status(self, job):
        contract_id = self._contract_id(job)
        if not contract_id:
            raise ValueError(
                "Dealwork contract id is required for status reconciliation"
            )

        return _data(
            self.client.get(
                f"/api/v1/contracts/{contract_id}"
            )
        )

    def get_payout(self, job):
        balance = _data(
            self.client.get("/api/v1/wallet/balance")
        )
        return {
            "balance": balance,
            "ready": False,
            "settled": False,
        }
