# Amarktai Earn — Implementation Status

This file is the single canonical implementation ledger for the revenue-launch work. Update this file as phases move forward. Do not create competing roadmap/status documents.

## Source of truth

- Repository: `amarktainetwork-blip/Amarktai-Earn`
- Base for Phase 1: `bfb22cedcf291d91cae8402fee1fd99652d9d109`
- Working branch: `phase1/revenue-funnel-shadow-providers`
- Production deployment is not changed by this branch.
- Economic truth: only authoritative final settlement is `SETTLED` cash.
- Safety truth: new channels remain fail-closed until account, policy, South African payout, economics, runtime and credential proofs pass.

## Phase map

1. **Phase 1 — Generic revenue funnel + shadow provider foundation**
   - Qualify demand before economics.
   - Score genuine buyer demand generically.
   - Run existing Profit Brain/acquisition preflight in SHADOW.
   - Add generic channel economics/execution-placement truth.
   - Catalog first priority revenue channels disabled/fail-closed.
2. **Phase 2 — Payment and treasury hub**
   - Add the canonical payment/settlement-provider abstraction and Banking/Money view.
   - Wire Paystack first when approved, then additional legal rails such as PayPal/Payoneer/Wise/local-bank-compatible routes as proven.
   - Signed webhooks, idempotency, reconciliation and settlement-state truth.
3. **Phase 3 — Activate first earning channels**
   - Finish owner onboarding/KYC/payout proof.
   - Connect/publish the highest-priority demand and supply channels one by one.
   - Keep first acquisitions/orders manually approved per market.
4. **Phase 4 — Controlled autonomous earning**
   - Market-by-market `SHADOW -> MANUAL -> LOW_RISK` promotion only after execution, QA and payout proof.
   - Scheduler, reconciliation, retry/rate-limit, observability and learning loops.
5. **Phase 5 — Distribution and scale**
   - Direct subscriptions/API products, reseller/white-label, automation/app ecosystems and measured infrastructure scaling.

## Current phase

**PHASE 1 — IN PROGRESS / IMPLEMENTED ON BRANCH / CI PENDING**

### Phase 1 DONE on working branch

- Created one generic demand qualification layer with explicit classifications:
  - `BUYER_DEMAND`
  - `SELLER_SUPPLY_LISTING`
  - `TEST_OR_SYNTHETIC`
  - `INCOMPLETE_REQUIREMENTS`
  - `UNFUNDED`
  - `UNSUPPORTED`
  - `UNKNOWN`
- Qualification is fail-closed; ambiguous inventory is not treated as buyer demand.
- Generic multi-market discovery now routes:
  `discover -> normalize -> ingest -> qualify -> shadow score -> existing acquisition preflight / Profit Brain`.
- Seller listings, unfunded work and ambiguous rows remain unscored and non-actionable.
- Genuine buyer demand receives conservative bootstrap economics and remains `WATCH`; no acquisition switch is enabled.
- Existing `MarketIntegrationProfile.evidence.catalog_truth` is reused for generic economics rather than adding a duplicate economics database model in Phase 1.
- Generic economics truth now supports:
  - percentage fee
  - fixed transaction fee
  - payout-cost reserve
  - FX-cost reserve
  - chargeback reserve
  - external execution cost
  - settlement-delay evidence
  - execution placement
- Added direct revenue-channel taxonomy:
  - `DIRECT_CHECKOUT`
  - `PAYMENT_LINK`
- Added execution-placement taxonomy:
  - `WEBDOCK_LIGHT`
  - `EXTERNAL_PROVIDER`
  - `APIFY`
  - `MANUAL`
  - `OFFHOST_REQUIRED`
  - `UNVERIFIED`
- Added first priority channels in SHADOW/fail-closed state:
  - Contra
  - RapidAPI
  - Apify Store
  - Lemon Squeezy Direct
- Apify is explicitly `execution_placement=APIFY`; scraper-heavy Actor work does not move onto Webdock.
- RapidAPI carries the currently captured 25% marketplace fee in economics evidence but remains economics/payout-unverified for LIVE operation.
- Lemon Squeezy is represented as direct commerce, not as posted-job demand.
- Paystack is deliberately **not** forced into the marketplace catalog. It belongs in the Phase 2 payment/settlement abstraction.
- Added deterministic qualification tests and integration coverage proving discovery can reach `EXPECTED` + `JobScore` + acquisition preflight while acquisition remains disabled.

### Phase 1 STILL TO DO

- Run hosted CI on the Phase 1 branch/PR.
- Fix any CI/test failure without broad refactors.
- Run migration check; Phase 1 is intended to require no database migration.
- Review the final diff for accidental duplicate architecture or unrelated changes.
- Merge only after CI is green.
- Deploy Phase 1 to production after merge.
- Prove against current real Dealwork inventory:
  - seller/service advertisements are filtered;
  - unfunded/incomplete rows are blocked;
  - genuine buyer demand becomes scored/`EXPECTED`;
  - acquisition remains blocked until manually enabled and payout proof exists.

### Owner actions / external blockers

These are not coding completion blockers for Phase 1 and must not be fabricated:

- Paystack account approval/KYC is in progress externally.
- Contra owner account, identity verification and actual South African payout options must be proven.
- RapidAPI provider account plus PayPal/South African withdrawal path must be proven.
- Apify creator KYC and payout route must be proven.
- Lemon Squeezy merchant KYC and actual South African bank payout account must be proven.

## Verification ledger

- Base branch checked: `main` at `bfb22cedcf291d91cae8402fee1fd99652d9d109` when Phase 1 started.
- Open PRs at Phase 1 start: none.
- Phase 1 working branch created cleanly from that base.
- Tests added/updated but hosted CI result is still pending.

## Next action

Open the Phase 1 pull request, run CI, fix only concrete failures, then merge/deploy and perform the real Dealwork shadow proof before starting Phase 2.
