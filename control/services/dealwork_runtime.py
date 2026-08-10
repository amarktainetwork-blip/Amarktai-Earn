from __future__ import annotations

from decimal import Decimal
import os

from control.models import Bid, Job, Marketplace
from control.services.acquisition_preflight import run_acquisition_preflight
from control.services.acquisition_runtime import AcquisitionError, acquire_profitable_job
from control.services.market_readiness import market_readiness
from control.services.markets import configured_adapter, sync_market_discovery


def _max_bids_per_cycle() -> int:
    try:
        value = int(os.getenv("DEALWORK_MAX_BIDS_PER_CYCLE", "1"))
    except (TypeError, ValueError):
        value = 1
    return max(0, min(value, 5))


def _qualified_buyer_demand(job: Job) -> bool:
    payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    qualification = payload.get("demand_qualification")
    return bool(
        isinstance(qualification, dict)
        and qualification.get("classification") == "BUYER_DEMAND"
        and qualification.get("actionable") is True
    )


def discover_dealwork(*, limit: int = 100) -> dict:
    """Run the canonical read-only Dealwork discovery/qualification pipeline."""
    result = sync_market_discovery("dealwork", limit=limit)
    return {**result, "mutation_performed": False}


def attempt_dealwork_bids(*, limit: int | None = None, adapter=None) -> dict:
    """Submit a bounded number of preflight-approved Dealwork bids.

    Open Dealwork jobs use the bid workflow. Claim is deliberately excluded from
    autonomous proving until an explicit remote claim-mode contract is represented
    in normalized job truth. Every candidate is re-preflighted immediately before
    the generic acquisition runtime obtains its lock and submits the remote bid.
    """
    market = Marketplace.objects.select_related("integration_profile").get(slug="dealwork")
    readiness = market_readiness(market)
    if not readiness["autonomy_ready"]:
        return {
            "attempted": 0,
            "submitted": 0,
            "blocked": 0,
            "enabled": False,
            "mutation_performed": False,
            "reason_codes": readiness["autonomy_blockers"],
        }

    max_cycle = _max_bids_per_cycle() if limit is None else max(0, min(int(limit), 5))
    if max_cycle <= 0:
        return {
            "attempted": 0,
            "submitted": 0,
            "blocked": 0,
            "enabled": True,
            "mutation_performed": False,
            "reason_codes": ["DEALWORK_BID_CYCLE_LIMIT_ZERO"],
        }

    adapter = adapter or configured_adapter("dealwork")
    attempted = submitted = blocked = 0
    jobs = (
        Job.objects.select_related("marketplace", "jobscore")
        .filter(
            marketplace=market,
            state=Job.State.EXPECTED,
            bids__isnull=True,
        )
        .order_by("-jobscore__expected_profit_per_minute", "-updated_at")
    )

    for job in jobs:
        if attempted >= max_cycle:
            break
        if not _qualified_buyer_demand(job):
            continue
        attempted += 1
        preflight = run_acquisition_preflight(job)
        if not preflight.allowed:
            blocked += 1
            continue
        score = getattr(job, "jobscore", None)
        offer = Decimal(str(getattr(score, "recommended_offer", None) or job.reward))
        if offer <= 0:
            blocked += 1
            continue
        try:
            acquire_profitable_job(
                adapter=adapter,
                job_id=job.id,
                node_id=os.getenv("NODE_ID", "VPS1"),
                action="BID",
                offered_price=offer,
            )
            submitted += 1
        except AcquisitionError:
            blocked += 1

    return {
        "attempted": attempted,
        "submitted": submitted,
        "blocked": blocked,
        "enabled": True,
        "mutation_performed": submitted > 0,
        "reason_codes": [],
    }


def run_dealwork_cycle(*, limit: int = 100) -> dict:
    discovery = discover_dealwork(limit=limit)
    acquisition = attempt_dealwork_bids()
    return {
        "enabled": True,
        "discovery": discovery,
        "acquisition": acquisition,
        "mutation_performed": bool(acquisition.get("mutation_performed")),
    }
