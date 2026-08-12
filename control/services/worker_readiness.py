from __future__ import annotations

import re

from workers.genx_support import catalog_supports


_GENX_CATEGORIES = {"text", "image", "audio", "video", "voice"}
_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(str(value or "").casefold()))


def _operation_category(contract: dict) -> str | None:
    """Resolve one operation's provider category from its canonical contract.

    Most operation contracts already expose a concrete GenX category.  The older
    generated-media WorkerSpec used the aggregate label ``media``; until that
    aggregate is split in the registry, operation semantics resolve its concrete
    GenX category deterministically rather than adding another worker-class map.
    """
    category = str(contract.get("provider_category") or "").casefold()
    if category in _GENX_CATEGORIES:
        return category
    operation = str(contract.get("operation") or "").casefold()
    words = set(_tokens(operation))
    if "voice" in words:
        return "voice"
    if "video" in words:
        return "video"
    if "music" in words or "audio" in words:
        return "audio"
    return None


def _operation_keywords(spec: dict, contract: dict) -> tuple[str, ...]:
    values = [
        str(contract.get("operation") or ""),
        str(spec.get("worker_class") or ""),
    ]
    tokens: list[str] = []
    for value in values:
        tokens.extend(_tokens(value.replace("_", " ")))
    # Conservative stems cover provider labels such as translate/translation and
    # transcribe/transcription without maintaining a second capability catalogue.
    for token in tuple(tokens):
        if len(token) >= 8:
            tokens.append(token[:7])
    return tuple(dict.fromkeys(token for token in tokens if len(token) >= 4))


def genx_catalog_supports_worker(spec: dict) -> bool:
    """Require live-catalog support for every provider-backed operation.

    The input is one row from ``workers.registry.registry_manifest``.  This keeps
    the registry as the capability source of truth and prevents owner/dashboard
    code from maintaining a parallel worker-to-provider dictionary.
    """
    contracts = [
        contract
        for contract in spec.get("operation_contracts", [])
        if str(contract.get("provider_category") or "").casefold() != "local"
        and contract.get("owner_action_blocker")
    ]
    if not contracts:
        return True
    for contract in contracts:
        category = _operation_category(contract)
        keywords = _operation_keywords(spec, contract)
        if not catalog_supports(*keywords, fallback_category=category):
            return False
    return True
