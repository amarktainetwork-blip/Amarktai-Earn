# Two-sided revenue engine

This repository supports one portfolio of economic work from two directions:

- demand-pull: posted jobs and bounties are normalized into `Job`;
- supply-push: authenticated, replay-bounded, idempotent `InboundOrder` records are normalized into the same `Job` lifecycle.

There is no second execution engine. Both paths use the worker registry, WorkPlan, execution, independent QA, submission/delivery, payout reconciliation, Profit Brain, capacity, and loss bounds. Targets remain objective floors and never cap positive expected profit. Only authoritative `SETTLED` payout evidence is received cash.

## Persisted truth

`ServiceOffering` represents a registered operation that may be sold. Registry presence alone is insufficient: `SELLABLE` requires a completed execution for the same operation and worker plus a persisted passing QA result. Coding and direct public-web offerings retain their existing runtime feature gates.

`MarketServiceListing` maps an offering to a marketplace. It stays blocked without current policy, verified seller capability, authentication, payout readiness, South African payout proof, Webdock-safe settlement, operation proof, and explicit publish switches.

`InboundOrder` maps one remote order and idempotency key to exactly one canonical `Job`. The internal receiver requires authenticated market identity, a replay-window timestamp, bounded JSON, safe pre-staged asset references, and matching digests for retries. No generic unauthenticated execution endpoint is exposed.

`InboundSettlementEvent` keeps authorization, escrow, pending, settled, and reversal evidence separate. Authorization and escrow never create settled cash. A payout becomes `SETTLED` only from an authoritative event and the canonical job follows `ACCEPTED → PAYOUT_PENDING → SETTLED`.

`PortfolioDecision` records a global ranking across posted work, bounties, and inbound orders. Ranking considers risk-adjusted profit, profit per productive minute, payment and acceptance probability, deadlines, reputation/learning value, concentration, and capacity. It does not privilege an integration by age.

## Integration truth

- Nevermined: the official HTTP/MCP/A2A and plan contract is represented locally. Webdock accepts only USD `FIXED_FIAT_PRICE` planning and requires separately verified Stripe Connect and South African payout evidence. No registration request is sent.
- Skyfire: official seller token claims are represented locally. `COIN` is rejected; only `CARD` or `BANK` can pass the local protocol boundary, and the channel remains blocked until the actual non-crypto payout route is externally proven. No token is charged.
- HYRVE: public service/job/Stripe/escrow claims are catalogued, but `source_wired=False` because a public automation contract has not been proven. No private dashboard API is used.
- Swarms: catalogued as agent/tool/prompt distribution only. It is not treated as a continuous posted-work feed, and publishing/payout remain externally blocked.
- AgentVerse.run: manual storefront/project/subscription candidate only; no mutation adapter is invented.
- AgentMarket and Chowdr: internal/test credits are non-cash and never enter revenue or payout accounting.
- Crypto/on-chain candidates: `OFFHOST_SETTLEMENT_REQUIRED`. `ExternalSettlementBridge` is an intentionally disabled future HTTPS boundary; this repository contains no Webdock wallet, chain, testnet, or transaction implementation.

All new credentials are blank and every publish/acquire/accept switch is off in `.env.example`. Catalog bootstrap creates disabled, payout-blocked, South-Africa-unverified records only.
