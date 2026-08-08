from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkloadDecision:
    allowed: bool
    reason_codes: tuple[str, ...]


_PROHIBITED = {
    "PROHIBITED_CRYPTO_MINING": (r"\bcrypto(?:currency)? mining\b", r"\bmine (?:bitcoin|ethereum|monero)\b"),
    "PROHIBITED_DEPIN": (r"\bdepin\b", r"decentralized physical infrastructure"),
    "PROHIBITED_BANDWIDTH_RESALE": (r"\b(?:bandwidth|residential proxy|proxy) resale\b", r"\bsell (?:unused )?bandwidth\b"),
    "PROHIBITED_UNAUTHORIZED_SCANNING": (r"\bunauthori[sz]ed (?:network )?(?:scan|scanning|pentest)\b", r"\bscan random (?:hosts|ips|networks)\b"),
    "PROHIBITED_SPAM": (r"\bspam campaign\b", r"\bmass unsolicited\b", r"\bbulk cold email scraping\b"),
    "PROHIBITED_FAKE_IDENTITY": (r"\bfake (?:identity|id|account)\b", r"\bimpersonat(?:e|ion)\b"),
    "PROHIBITED_FRAUD": (r"\b(?:commit|enable|facilitate) fraud\b", r"\bstolen (?:card|credential)s?\b"),
    "PROHIBITED_LOCAL_INFERENCE": (r"\b(?:run|host|deploy) (?:an? )?(?:llm|language model) locally\b", r"\blocal gpu inference\b"),
    "PROHIBITED_BROWSER_AUTOMATION": (r"\buncontrolled browser automation\b", r"\bautomatically (?:click|browse|purchase) without limits\b"),
}


def job_text(job) -> str:
    payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    values = [job.title, job.task_class]
    for key in ("description", "requirements", "instructions", "task", "deliverables"):
        value = payload.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(str(item) for item in value)
    return "\n".join(values).casefold()


def evaluate_text(text: str) -> WorkloadDecision:
    normalized = str(text or "").casefold()
    reasons = [code for code, patterns in _PROHIBITED.items() if any(re.search(pattern, normalized) for pattern in patterns)]
    return WorkloadDecision(not reasons, tuple(reasons))


def evaluate_job(job) -> WorkloadDecision:
    reasons = list(evaluate_text(job_text(job)).reason_codes)
    latest_policy = job.marketplace.policy_versions.order_by("-checked_at", "-created_at").first()
    if latest_policy is not None and not latest_policy.automation_allowed:
        reasons.append("PROHIBITED_MARKET_POLICY")
    return WorkloadDecision(not reasons, tuple(reasons))


def require_allowed(job) -> None:
    decision = evaluate_job(job)
    if not decision.allowed:
        raise ValueError("workload prohibited: " + ",".join(decision.reason_codes))
