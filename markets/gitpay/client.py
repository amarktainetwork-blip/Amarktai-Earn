from __future__ import annotations

from decimal import Decimal
from typing import Callable

from markets.base import MarketCapabilities, NormalizedOpportunity
from markets.normalization import decimal_reward, first_value
from markets.source_workflow import SourceWorkflowAdapter


class GitpayAdapter(SourceWorkflowAdapter):
    """First-party source import with assignment-gated owner workflow truth."""

    slug = "gitpay"
    capabilities = MarketCapabilities(
        discover=True,
        normalize=True,
        application=False,
        assignment_status=False,
        delivery=False,
        payment_request=False,
        status=True,
        settlement_status=True,
        payout=True,
        policy_verified=True,
        payout_ready=False,
    )

    def __init__(self, source_reader: Callable[..., list[dict]] | None = None):
        super().__init__(source_reader)

    def normalize_job(self, raw: dict) -> NormalizedOpportunity:
        assigned = bool(first_value(raw, "assigned", "assignment_confirmed", default=False))
        return NormalizedOpportunity(
            external_id=str(first_value(raw, "id", "task_id", "issue_url")),
            title=str(first_value(raw, "title", "issue_title", default="Untitled Gitpay task")),
            task_class=str(first_value(raw, "type", "category", "language", default="coding")),
            reward=decimal_reward(raw, "reward", "amount", "value"),
            currency=str(first_value(raw, "currency", default="USD"))[:3].upper(),
            raw={**raw, "assignment_confirmed": assigned},
            action="GITPAY_TASK",
            fee_rate=Decimal(str(first_value(raw, "fee_rate", default="0"))),
            payout_probability=Decimal(str(first_value(raw, "payout_probability", default="0.65"))),
            acceptance_probability=Decimal(str(first_value(raw, "acceptance_probability", default="0.55"))),
            expected_minutes=int(first_value(raw, "expected_minutes", default=90)),
            capabilities_required=("coding", "git", "tests", "explicit_assignment"),
        )

    def eligibility(self, opportunity: NormalizedOpportunity) -> dict:
        assigned = opportunity.raw.get("assignment_confirmed") is True
        return {
            "eligible": assigned,
            "provider_spend_allowed": assigned,
            "reason_codes": [] if assigned else ["WAITING_FOR_EXPLICIT_ASSIGNMENT"],
        }

    def get_status(self, job):
        return {
            "assignment_confirmed": job.raw.get("assignment_confirmed") is True,
            "settled": False,
            "reason": "Assignment, acceptance, payment request, and bank/PayPal settlement remain distinct.",
        }

    def get_payout(self, job):
        return {"ready": False, "settled": False, "rails": ["BANK", "PAYPAL"], "owner_action": True}
