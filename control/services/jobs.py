import os
from decimal import Decimal, ROUND_CEILING
from django.db import transaction
from control.acquisition import AcquisitionThresholds, MarketGate, ScoreGate, acquisition_gate
from control.economics import EconomicsInput, score_job
from control.job_state import assert_transition
from control.models import AuditEvent, Job, JobScore, Marketplace
from markets.base import NormalizedOpportunity


def _thresholds() -> AcquisitionThresholds:
    return AcquisitionThresholds(
        min_expected_profit=Decimal(os.getenv("MIN_EXPECTED_PROFIT_USD", "1.00")),
        min_expected_profit_per_minute=Decimal(os.getenv("MIN_EXPECTED_PROFIT_PER_MINUTE_USD", "0.05")),
        max_genx_cost=Decimal(os.getenv("MAX_GENX_COST_PER_JOB_USD", "2.00")),
    )


@transaction.atomic
def ingest_opportunity(marketplace: Marketplace, opportunity: NormalizedOpportunity) -> tuple[Job, bool]:
    job, created = Job.objects.update_or_create(
        marketplace=marketplace,
        external_id=opportunity.external_id,
        defaults={
            "title": opportunity.title[:500],
            "task_class": opportunity.task_class[:100],
            "reward": opportunity.reward,
            "currency": opportunity.currency[:3],
            "normalized_payload": opportunity.raw,
        },
    )
    AuditEvent.objects.create(
        event_type="job.discovered" if created else "job.refreshed",
        actor="market-scout",
        metadata={"job_id": str(job.id), "market": marketplace.slug, "external_id": opportunity.external_id},
    )
    return job, created


@transaction.atomic
def score_and_persist(
    job: Job,
    economics: EconomicsInput,
    decision: str = "WATCH",
    reason_codes: list[str] | None = None,
    max_genx_credits: Decimal = Decimal("0"),
) -> JobScore:
    result = score_job(economics)
    score, _ = JobScore.objects.update_or_create(
        job=job,
        defaults={
            "p_acquire": economics.p_acquire,
            "p_accept": economics.p_accept,
            "p_payment": economics.p_payment,
            "expected_genx_cost": economics.expected_genx_cost,
            "expected_external_cost": economics.expected_external_cost,
            "expected_cash": result.expected_cash,
            "expected_profit": result.expected_profit,
            "expected_profit_per_minute": result.expected_profit_per_minute,
            "expected_profit_per_genx_credit": result.expected_profit_per_genx_credit,
            "expected_minutes": max(1, int(economics.estimated_worker_minutes.to_integral_value(rounding=ROUND_CEILING))),
            "max_genx_credits": max_genx_credits,
            "decision": decision,
            "reason_codes": reason_codes or [],
        },
    )
    if job.state == Job.State.DISCOVERED:
        job.state = Job.State.EXPECTED
        job.save(update_fields=["state", "updated_at"])
    return score


def acquisition_decision(job: Job):
    score = job.jobscore
    return acquisition_gate(
        MarketGate(
            enabled=job.marketplace.enabled,
            status=job.marketplace.status,
            payout_ready=job.marketplace.payout_ready,
            south_africa_verified=job.marketplace.south_africa_verified,
        ),
        ScoreGate(
            expected_profit=score.expected_profit,
            expected_profit_per_minute=score.expected_profit_per_minute,
            expected_genx_cost=score.expected_genx_cost,
        ),
        _thresholds(),
    )


@transaction.atomic
def transition_job(job_id, target_state: str, actor: str = "controller", metadata: dict | None = None) -> Job:
    job = Job.objects.select_for_update().get(pk=job_id)
    previous = job.state
    assert_transition(previous, target_state)
    if previous != target_state:
        job.state = target_state
        job.save(update_fields=["state", "updated_at"])
        AuditEvent.objects.create(
            event_type="job.state_changed",
            actor=actor,
            metadata={"job_id": str(job.id), "from": previous, "to": target_state, **(metadata or {})},
        )
    return job