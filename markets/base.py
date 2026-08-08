from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

@dataclass(frozen=True)
class MarketCapabilities:
    discover: bool = True
    claim: bool = False
    bid: bool = False
    apply: bool = False
    messages: bool = False
    submit: bool = False
    payout: bool = False
    webhooks: bool = False

@dataclass(frozen=True)
class NormalizedOpportunity:
    external_id: str
    title: str
    task_class: str
    reward: Decimal
    currency: str = "USD"
    raw: dict[str, Any] = field(default_factory=dict)

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
