# Two-sided revenue engine

This repository supports one portfolio of economic work from two directions:

- demand-pull: posted jobs and bounties are normalized into `Job`;
- supply-push: authenticated, replay-bounded, idempotent `InboundOrder` records are normalized into the same `Job` lifecycle.

There is no second execution engine. Both paths use the worker registry, WorkPlan, execution, independent QA, submission/delivery, payout reconciliation, Profit Brain, capacity, and loss bounds. Targets remain objective floors and never cap positive expected profit. Only authoritative `SETTLED` payout evidence is received cash.

## Persisted truth

`ServiceOffering` represents a registered operation that may be sold. Registry and source truth produce `SOURCE_PROVEN`; a completed matching execution plus independent passing QA produces `EXECUTION_PROVEN`; only a later runtime-safety evaluation can produce `SELLABLE`. Coding remains `EXECUTION_PROVEN` while sandbox coding is off, and direct public-web work remains `EXECUTION_PROVEN` while public-web data is off. Owner sellable counts also require explicit enablement and order acceptance.

`MarketServiceListing` maps an offering to a marketplace. It stays blocked without current policy, verified seller capability, authentication, payout readiness, South African payout proof, Webdock-safe settlement, operation proof, and explicit publish switches.

`InboundOrder` maps one remote order and idempotency key to exactly one canonical `Job`. The internal receiver requires authenticated market identity, a replay-window timestamp, bounded JSON, safe pre-staged asset references, and matching digests for retries. No generic unauthenticated execution endpoint is exposed.

`InboundSettlementEvent` keeps authorization, escrow, pending, settled, and reversal evidence separate. Authorization and escrow never create payout cash records. Authoritative pending, settlement, and reversal events reuse `record_payout_state`, preserving amount-mutation protection, append-only ledger evidence, treasury recomputation, and canonical job transitions. Conflicting reuse of a remote event ID fails closed. Reversal preserves historical settlement evidence while removing the payout from settled cash.

`PortfolioDecision` records a global ranking across posted work, bounties, and inbound orders. Ranking remains broad, including profitable blocked work for SHADOW visibility. `selected=True` is narrower: it requires a current eligible and action-allowed preflight, current market/payout/South-Africa/policy truth, relevant runtime switches, and available slots/minutes. `would_select_if_enabled` preserves shadow planning without pretending an action-disabled job is scheduled.

## Integration truth

- Nevermined: the official HTTP/MCP/A2A and plan contract is represented locally. Webdock accepts only USD `FIXED_FIAT_PRICE` planning and requires separately verified Stripe Connect and South African payout evidence. No registration request is sent.
- Skyfire: official seller token claims are represented locally. `COIN` is rejected; only `CARD` or `BANK` can pass the local protocol boundary, and the channel remains blocked until the actual non-crypto payout route is externally proven. No token is charged.
- HYRVE: public service/job/Stripe/escrow claims are catalogued, but `source_wired=False` because a public automation contract has not been proven. No private dashboard API is used.
- Swarms: catalogued as agent/tool/prompt distribution only. It is not treated as a continuous posted-work feed, and publishing/payout remain externally blocked.
- AgentVerse.run: manual storefront/project/subscription candidate only; no mutation adapter is invented.
- AgentMarket and Chowdr: internal/test credits are non-cash and never enter revenue or payout accounting.
- Chain, token-reward, wallet, and x402 candidates are absent from the revenue catalogue. Defensive host-policy identifiers remain only to reject stale or future attempts to reintroduce them.

All new credentials are blank and every publish/acquire/accept switch is off in `.env.example`. Catalog bootstrap creates disabled, payout-blocked, South-Africa-unverified records only. Its catalog-owned-field enrichment backfills the canonical work profiles, including assignment-gated Gitpay, without overwriting marketplace enablement, payout/South-Africa state, autonomous acquisition, operator evidence, blockers, or payout proof; repeat runs are idempotent.
