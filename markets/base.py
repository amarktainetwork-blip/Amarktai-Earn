from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

@dataclass(frozen=True)
class MarketCapabilities:
    discover: bool = True
    normalize: bool = True
    claim: bool = False
    bid: bool = False
    apply: bool = False
    messages: bool = False
    input_assets: bool = False
    submission: bool = False
    revision: bool = False
    status: bool = False
    payment: bool = False
    submit: bool = False
    payout: bool = False
    webhook_or_event_support: bool = False
    rate_limit: bool = False
    policy_verified: bool = False
    payout_ready: bool = False
    webhooks: bool = False
    repo_access: bool = False
    github_try: bool = False
    github_claim: bool = False
    application: bool = False
    assignment_status: bool = False
    delivery: bool = False
    payment_request: bool = False
    settlement_status: bool = False

    def as_dict(self) -> dict[str, bool]:
        """Return the canonical capability contract, retaining legacy aliases."""
        values = {
            "discover": self.discover,
            "normalize": self.normalize,
            "claim": self.claim,
            "apply": self.apply,
            "bid": self.bid,
            "messages": self.messages,
            "input_assets": self.input_assets,
            "submission": self.submission or self.submit,
            "revision": self.revision,
            "status": self.status,
            "payment": self.payment,
            "payout": self.payout,
            "webhook_or_event_support": self.webhook_or_event_support or self.webhooks,
            "rate_limit": self.rate_limit,
            "policy_verified": self.policy_verified,
            "payout_ready": self.payout_ready,
            "repo_access": self.repo_access or self.input_assets,
            "github_try": self.github_try,
            "github_claim": self.github_claim,
            "application": self.application or self.apply,
            "assignment_status": self.assignment_status,
            "delivery": self.delivery or self.submission or self.submit,
            "payment_request": self.payment_request,
            "settlement_status": self.settlement_status or self.payout,
        }
        return values

@dataclass(frozen=True)
class NormalizedOpportunity:
    external_id: str
    title: str
    task_class: str
    reward: Decimal
    currency: str = "USD"
    raw: dict[str, Any] = field(default_factory=dict)
    action: str = "FUNDED_FEATURE_WORK"
    fee_rate: Decimal = Decimal("0")
    payout_probability: Decimal = Decimal("0.5")
    acceptance_probability: Decimal = Decimal("0.5")
    expected_provider_cost: Decimal = Decimal("0")
    expected_execution_cost: Decimal = Decimal("0")
    expected_minutes: int = 60
    source_classification: str = "MARKETPLACE_DISCOVERY"
    competition: dict[str, Any] = field(default_factory=dict)
    capabilities_required: tuple[str, ...] = ()

class MarketAdapter(ABC):
    slug: str
    capabilities: MarketCapabilities
    @abstractmethod
    def health(self) -> dict: ...
    @abstractmethod
    def payout_status(self) -> dict: ...
    @abstractmethod
    def discover_jobs(self) -> list[dict]: ...
    @abstractmethod
    def normalize_job(self, raw: dict) -> NormalizedOpportunity: ...
    def claim(self, job): raise NotImplementedError
    def bid(self, job, amount): raise NotImplementedError
    def apply(self, job, amount, message): raise NotImplementedError
    def get_messages(self, job): raise NotImplementedError
    def submit(self, job, artifact): raise NotImplementedError
    def get_status(self, job): raise NotImplementedError
    def get_payout(self, job): raise NotImplementedError
    def discover(self, **filters):
        return self.discover_jobs(**filters)
    def normalize(self, raw: dict) -> NormalizedOpportunity:
        return self.normalize_job(raw)
    def eligibility(self, opportunity: NormalizedOpportunity) -> dict:
        return {"eligible": True, "reason_codes": []}
    def status(self, job):
        return self.get_status(job)
    def reconcile(self, job):
        return {"status": self.get_status(job), "payout": self.get_payout(job)}
