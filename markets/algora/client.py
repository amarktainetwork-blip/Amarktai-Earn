from markets.base import MarketCapabilities, NormalizedOpportunity
from markets.normalization import decimal_reward, first_value
from markets.source_workflow import SourceWorkflowAdapter


class AlgoraAdapter(SourceWorkflowAdapter):
    slug = "algora"
    capabilities = MarketCapabilities(
        discover=True, normalize=True, status=True, payment=True, payout=True,
        policy_verified=True, payout_ready=False,
    )

    def normalize_job(self, raw: dict) -> NormalizedOpportunity:
        task = raw.get("task") if isinstance(raw.get("task"), dict) else {}
        combined = {**raw, **{f"task_{key}": value for key, value in task.items()}}
        reward = decimal_reward(raw, "reward", "amount", "reward_amount")
        if reward == 0:
            reward = decimal_reward(raw, "reward_cents", cents=True)
        return NormalizedOpportunity(
            external_id=str(first_value(combined, "id", "bounty_id", "task_id", "task_url")),
            title=str(first_value(combined, "task_title", "title", default="Untitled Algora bounty")),
            task_class=str(first_value(combined, "task_repo_name", "category", "language", default="coding")),
            reward=reward,
            currency=str(first_value(raw, "currency", default="USD"))[:3].upper(),
            raw=raw,
        )

    def get_status(self, job):
        return {"source": job.raw, "settled": False, "reason": "Claim/PR state is not payout settlement proof"}

    def get_payout(self, job):
        return {"ready": False, "settled": False, "reason": "Algora payout onboarding and transfer require external reconciliation"}
