from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, GenXCreditValuation


FOUR_PLACES = Decimal("0.0001")


@dataclass(frozen=True)
class ResolvedCreditCost:
    amount: Decimal
    currency: str
    valuation_id: str
    version: str
    source: str
    monetary_cost_per_credit: Decimal
    effective_at: str


def current_credit_valuation(*, currency: str, at=None) -> GenXCreditValuation | None:
    """Return the latest verified valuation effective at the economic event timestamp."""
    at = at or timezone.now()
    return (
        GenXCreditValuation.objects.filter(
            currency=str(currency).upper()[:3],
            verified=True,
            active=True,
            effective_at__lte=at,
        )
        .order_by("-effective_at", "-created_at")
        .first()
    )


def monetary_cost_for_credits(*, credits: Decimal, currency: str, at=None) -> ResolvedCreditCost | None:
    valuation = current_credit_valuation(currency=currency, at=at)
    if valuation is None:
        return None
    amount = (Decimal(credits) * valuation.monetary_cost_per_credit).quantize(
        FOUR_PLACES, rounding=ROUND_HALF_UP
    )
    return ResolvedCreditCost(
        amount=amount,
        currency=valuation.currency,
        valuation_id=str(valuation.id),
        version=valuation.version,
        source=valuation.source,
        monetary_cost_per_credit=valuation.monetary_cost_per_credit,
        effective_at=valuation.effective_at.isoformat(),
    )


@transaction.atomic
def record_credit_valuation(
    *,
    version: str,
    currency: str,
    monetary_cost_per_credit: Decimal,
    source: str,
    effective_at,
    evidence: dict,
    verified: bool,
    actor: str,
) -> GenXCreditValuation:
    """Persist owner/provider evidence; callers may never synthesize a default conversion."""
    version = str(version or "").strip()[:80]
    currency = str(currency or "").strip().upper()[:3]
    source = str(source or "").strip()[:255]
    cost = Decimal(monetary_cost_per_credit)
    if not version or len(currency) != 3 or not source or cost <= 0:
        raise ValueError("GENX_CREDIT_VALUATION_EVIDENCE_INVALID")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("GENX_CREDIT_VALUATION_EVIDENCE_REQUIRED")
    row = GenXCreditValuation.objects.create(
        version=version,
        currency=currency,
        monetary_cost_per_credit=cost,
        source=source,
        evidence=evidence,
        effective_at=effective_at,
        verified=bool(verified),
        active=True,
    )
    AuditEvent.objects.create(
        event_type="genx.credit_valuation_recorded",
        actor=str(actor)[:120],
        metadata={
            "valuation_id": str(row.id),
            "version": row.version,
            "currency": row.currency,
            "source": row.source,
            "verified": row.verified,
            "effective_at": row.effective_at.isoformat(),
        },
    )
    return row
