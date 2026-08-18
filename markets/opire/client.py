from decimal import Decimal

from markets.base import MarketCapabilities, NormalizedOpportunity
from markets.normalization import decimal_reward, first_value
from markets.source_workflow import SourceWorkflowAdapter


class OpireAdapter(SourceWorkflowAdapter):
    slug = "opire"
    capabilities = MarketCapabilities(
        discover=True, github_try=True, github_claim=True, status=True, payment=True, payout=True,
        settlement_status=True,
        policy_verified=True, payout_ready=False,
    )

    def normalize_job(self, raw: dict) -> NormalizedOpportunity:
        return NormalizedOpportunity(
            external_id=str(first_value(raw, "id", "reward_id", "issue_url")),
            title=str(first_value(raw, "title", "issue_title", default="Untitled Opire reward")),
            task_class=str(first_value(raw, "technology", "language", "category", default="coding")),
            reward=decimal_reward(raw, "reward", "amount", "total_reward"),
            currency=str(first_value(raw, "currency", default="USD"))[:3].upper(),
            raw=raw,
            action="OPIRE_REWARD",
            fee_rate=Decimal(str(first_value(raw, "fee_rate", default="0"))),
            payout_probability=Decimal(str(first_value(raw, "payout_probability", default="0.65"))),
            acceptance_probability=Decimal(str(first_value(raw, "acceptance_probability", default="0.55"))),
            expected_minutes=int(first_value(raw, "expected_minutes", default=90)),
            capabilities_required=("coding", "git", "tests", "github_comments"),
        )

    @staticmethod
    def github_try_command() -> str:
        return "/try"

    @staticmethod
    def github_claim_command(issue_number: int) -> str:
        if int(issue_number) < 1:
            raise ValueError("Opire claim requires a positive issue number")
        return f"/claim #{int(issue_number)}"

    def get_status(self, job):
        return {
            "source": job.raw,
            "settled": False,
            "reason": "A tried, solved, merged, or claimed reward is not payment proof.",
        }

    def get_payout(self, job):
        return {"ready": False, "settled": False, "reason": "Reward creator payment must be reconciled separately"}
