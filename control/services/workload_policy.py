from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkloadDecision:
    allowed: bool
    reason_codes: tuple[str, ...]


# Host-level workload guardrails. These deliberately target execution intent,
# not mere subject matter: writing about Bitcoin or receiving an off-host USDC
# payout is different from operating blockchain infrastructure on this VPS.
_PROHIBITED = {
    "PROHIBITED_CRYPTO_MINING": (
        r"\bcrypto(?:currency)? mining\b",
        r"\bmine (?:bitcoin|ethereum|monero|cryptocurrency|crypto)\b",
        r"\b(?:xmrig|cryptominer|crypto miner)\b",
    ),
    "PROHIBITED_BLOCKCHAIN_RUNTIME": (
        r"\b(?:run|host|deploy|operate|start|maintain|sync)\b.{0,60}\b(?:blockchain|crypto(?:currency)? node|bitcoin node|ethereum node|solana node|validator node|testnet|mainnet node)\b",
        r"\b(?:blockchain|bitcoin|ethereum|solana|crypto(?:currency)?)\b.{0,45}\b(?:node|validator|testnet|chain daemon)\b",
        r"\b(?:wallet signer|wallet daemon|on[- ]chain signer|sign blockchain transactions? on (?:the )?(?:server|vps|host))\b",
    ),
    "PROHIBITED_DEPIN": (
        r"\bdepin\b",
        r"decentralized physical infrastructure",
    ),
    "PROHIBITED_BANDWIDTH_RESALE": (
        r"\b(?:bandwidth|residential proxy|proxy) resale\b",
        r"\bsell (?:unused )?bandwidth\b",
        r"\b(?:open|residential) proxy service\b",
    ),
    "PROHIBITED_NETWORK_SCANNING": (
        r"\b(?:network|port|host|internet[- ]wide|vulnerability) scan(?:ner|ning)?\b",
        r"\b(?:nmap|masscan|zmap)\b",
        r"\bscan (?:random |public |internet )?(?:hosts|ips|networks|ports)\b",
    ),
    "PROHIBITED_STRESS_TESTING": (
        r"\b(?:stress|load|flood) test(?:ing)?\b",
        r"\b(?:http|network|traffic) flood(?:ing)?\b",
        r"\b(?:ddos|dos) simulation\b",
    ),
    "PROHIBITED_SPAM": (
        r"\bspam campaign\b",
        r"\bmass unsolicited\b",
        r"\bbulk cold email scraping\b",
        r"\bunsolicited bulk e[- ]?mail\b",
        r"\bsend (?:bulk|mass) unsolicited (?:email|mail|messages?)\b",
    ),
    "PROHIBITED_TOR_RELAY": (
        r"\btor relay\b",
        r"\btor exit node\b",
    ),
    "PROHIBITED_TORRENT": (
        r"\b(?:torrent|bittorrent) (?:seed|seeding|download|downloading|client|server)\b",
        r"\bseed torrents?\b",
    ),
    "PROHIBITED_MEDIA_STREAMING_SERVER": (
        r"\b(?:host|run|deploy|operate)\b.{0,45}\b(?:media|video|audio|live) streaming (?:server|service|relay)\b",
        r"\b(?:restream|re-stream) copyrighted\b",
    ),
    "PROHIBITED_CONTINUOUS_SCRAPING": (
        r"\b(?:continuous|continuously|constant|24/7|high[- ]volume|massive)\b.{0,35}\b(?:scrap(?:e|er|ing)|crawl(?:er|ing)?)\b",
        r"\b(?:scrap(?:e|ing)|crawl(?:ing)?)\b.{0,45}\b(?:millions of pages|entire internet|nonstop|without rate limits)\b",
    ),
    "PROHIBITED_COPYRIGHT_SCRAPING": (
        r"\bscrap(?:e|ing)\b.{0,45}\b(?:copyrighted|paywalled|pirated)\b",
        r"\b(?:download|mirror|copy)\b.{0,45}\bpaywalled content\b",
    ),
    "PROHIBITED_COPYRIGHT_DISTRIBUTION": (
        r"\b(?:distribute|redistribute|host|mirror|share)\b.{0,45}\b(?:pirated|copyrighted) (?:movies?|music|books?|software|content|material)\b",
        r"\b(?:piracy|warez) (?:site|mirror|distribution)\b",
    ),
    "PROHIBITED_TRAFFIC_EXCHANGE": (
        r"\btraffic exchange\b",
        r"\bautosurf\b",
    ),
    "PROHIBITED_LOCAL_NEURAL_RUNTIME": (
        r"\b(?:run|host|deploy|serve)\b.{0,55}\b(?:neural net(?:work)?|llm|language model|ai model)\b.{0,35}\b(?:locally|on (?:the )?(?:vps|server|host)|self[- ]hosted)\b",
        r"\b(?:local|self[- ]hosted)\b.{0,30}\b(?:gpu )?(?:llm|neural net(?:work)?|model) (?:inference|server|runtime)\b",
        r"\b(?:train|training|fine[- ]?tune|fine[- ]?tuning)\b.{0,55}\b(?:neural net(?:work)?|llm|ai model)\b.{0,35}\b(?:on (?:the )?(?:vps|server|host)|locally)\b",
    ),
    "PROHIBITED_FAKE_IDENTITY": (
        r"\bfake (?:identity|id|account)\b",
        r"\bimpersonat(?:e|ion)\b",
    ),
    "PROHIBITED_FRAUD": (
        r"\b(?:commit|enable|facilitate) fraud\b",
        r"\bstolen (?:card|credential)s?\b",
    ),
    "PROHIBITED_BROWSER_AUTOMATION": (
        r"\buncontrolled browser automation\b",
        r"\bautomatically (?:click|browse|purchase) without limits\b",
    ),
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
    return WorkloadDecision(not reasons, tuple(dict.fromkeys(reasons)))


def require_allowed(job) -> None:
    decision = evaluate_job(job)
    if not decision.allowed:
        raise ValueError("workload prohibited: " + ",".join(decision.reason_codes))
