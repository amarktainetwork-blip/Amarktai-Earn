from markets.base import MarketCapabilities, NormalizedOpportunity
from markets.normalization import decimal_reward, first_value
from markets.source_workflow import SourceWorkflowAdapter


class OpireAdapter(SourceWorkflowAdapter):
    slug = "opire"
    capabilities = MarketCapabilities(
        discover=True, status=True, payment=True, payout=True,
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
        )

    def get_status(self, job):
        return {
            "source": job.raw,
            "settled": False,
            "reason": "A tried, solved, merged, or claimed reward is not payment proof.",
        }

    def get_payout(self, job):
        return {"ready": False, "settled": False, "reason": "Reward creator payment must be reconciled separately"}
