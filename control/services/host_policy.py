from __future__ import annotations

import os
from collections.abc import Mapping


WEBDOCK_PROVIDER = "webdock"

# Defensive deny-list for identifiers removed from every earning catalogue.
# Keeping the identifiers here prevents stale database rows or future code from
# reintroducing them into the Webdock runtime.
WEBDOCK_OFFHOST_ONLY_MARKETS = frozenset({
    "virtuals-acp",
    "coinbase-x402-bazaar",
    "okx-ai",
    "agrenting",
    "olas-mech",
    "masumi-sokosumi",
    "singularitynet",
    "fetch-agentverse",
    "clawrr",
    "planetloga",
})

# Explicit feature switches which are never permitted to be enabled on Webdock.
# They are intentionally named here even when no implementation currently exists:
# production preflight then becomes a tripwire against future accidental additions.
WEBDOCK_PROHIBITED_RUNTIME_SWITCHES = (
    "BLOCKCHAIN_LOCAL_RUNTIME_ENABLED",
    "CRYPTO_NODE_ENABLED",
    "CRYPTO_MINING_ENABLED",
    "DEPIN_NODE_ENABLED",
    "WALLET_SIGNER_ENABLED",
    "TESTNET_RUNTIME_ENABLED",
    "LOCAL_NEURAL_RUNTIME_ENABLED",
    "LOCAL_MODEL_SERVER_ENABLED",
    "NEURAL_NET_TRAINING_ENABLED",
    "NETWORK_SCANNER_ENABLED",
    "STRESS_TEST_ENABLED",
    "TOR_RELAY_ENABLED",
    "TORRENT_ENABLED",
    "MEDIA_STREAMING_SERVER_ENABLED",
    "CONTINUOUS_SCRAPER_ENABLED",
    "TRAFFIC_EXCHANGE_ENABLED",
    "BULK_UNSOLICITED_EMAIL_ENABLED",
    "BANDWIDTH_RESALE_ENABLED",
    "OPEN_PROXY_ENABLED",
)


def host_provider(environ: Mapping[str, str] | None = None) -> str:
    source = environ if environ is not None else os.environ
    return str(source.get("HOST_PROVIDER", WEBDOCK_PROVIDER) or WEBDOCK_PROVIDER).strip().casefold()


def _truthy(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on", "enabled"}


def market_runtime_compatible(market_slug: str, *, provider: str | None = None) -> bool:
    active_provider = (provider or host_provider()).strip().casefold()
    if active_provider != WEBDOCK_PROVIDER:
        return True
    return str(market_slug or "").strip().casefold() not in WEBDOCK_OFFHOST_ONLY_MARKETS


def runtime_policy_errors(environ: Mapping[str, str] | None = None) -> list[str]:
    source = environ if environ is not None else os.environ
    if host_provider(source) != WEBDOCK_PROVIDER:
        return []
    errors: list[str] = []
    for name in WEBDOCK_PROHIBITED_RUNTIME_SWITCHES:
        if _truthy(source.get(name)):
            errors.append(f"{name} cannot be enabled on Webdock")
    return errors
