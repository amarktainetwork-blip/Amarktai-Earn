# Amarktai Earn

**Autonomous Digital Income Operating System**
Production domain: `https://earn.amarktai.co.za`
Repository: `Amarktai-Earn`
Primary deployment: one Webdock VPS, expandable into one centrally controlled multi-node fleet.

This README is the primary source of truth. Supporting documents may explain implementation details, but they may not redefine the product, revenue model, security boundaries, money states, or deployment architecture described here.

## 1. What Amarktai Earn is

Amarktai Earn is a private owner-only autonomous digital-work operating system. It discovers legitimate paid digital work, evaluates expected profit, acquires only permitted and economically justified work, executes it, independently QA-checks it, repairs failures, submits deliverables, handles routine revisions, tracks acceptance and payouts, and learns which markets, task classes, workers, and GenX models produce the best settled net profit.

It is not a public freelance marketplace, a consumer chatbot, a speculative income-scheme aggregator, or a dashboard that depends on daily manual job searching. The normal operating model is autonomous observation by the owner after required account creation, KYC, payout onboarding, and security setup.

## 2. Financial mission

The optimisation target is **NET SETTLED PROFIT**, not gross bounty value, CPU utilisation, application count, agent count, or GenX request volume.

Milestones are targets, not guarantees:

1. first real autonomous paid completion;
2. $20/day rolling net;
3. $30/day rolling net;
4. $50/day rolling net;
5. $100–$133/day cluster-wide;
6. approximately $3,000–$4,000/month net after ordinary system expenses when economics support it.

No estimate may be presented as received cash. Only the `SETTLED` state is actual received money.

## 3. Architecture

VPS1 is the permanent logical hub and owns the dashboard, PostgreSQL, Redis, scheduler, Money Brain, marketplace gateways, GenX gateway, GitHub gateway, Treasury, global job locks, node registry, central reporting, and security control plane.

External opportunities enter through VPS1 only:

`Marketplace -> VPS1 gateway -> normalize -> policy gate -> economic score -> global lock -> scheduler -> worker -> independent QA -> repair if needed -> submit -> revision/payment watchers -> ledger/dashboard`

Future worker VPSs execute assigned work but do not independently poll marketplaces and do not receive broad marketplace, GenX, GitHub, or treasury master credentials.

## 4. Webdock restrictions

The Webdock deployment excludes completely:

- cryptocurrency mining, blockchain nodes, validators, staking, testnets, DePIN compute/storage;
- bandwidth resale, residential proxies, traffic exchanges, packet sharing, Tor, torrenting;
- network scanning, automated vulnerability scanning, unauthorised security testing, DDoS/stress testing;
- sustained heavy third-party scraping;
- prohibited local neural-network training or inference;
- spam, unsolicited bulk messaging, fake traffic, ad-click automation, survey manipulation;
- fake marketplace accounts, fake identity/address workarounds, deceptive affiliate systems;
- any workload that risks the Webdock account.

Incompatible future business lines belong in a separate repository and compliant host. Disabled crypto modules are intentionally absent here.

## 5. Technology stack

V1 uses:

- Python 3.12;
- Django control plane/dashboard;
- PostgreSQL as economic source of truth;
- Redis + RQ for prioritized durable work queues;
- Docker for isolated disposable job execution;
- Caddy for TLS/reverse proxy;
- Argon2id password hashing;
- JWT access/refresh session architecture;
- TOTP and one-time recovery codes;
- Pydantic/PydanticAI for typed decision orchestration where AI decisions are needed;
- official MCP Python SDK where a marketplace exposes MCP;
- Aider for small/medium coding jobs;
- OpenHands Software Agent SDK for heavy coding jobs;
- pandas/Polars/DuckDB/openpyxl/jq for deterministic data tasks;
- FFmpeg for permitted light media manipulation;
- Playwright/Browser Use only as an explicitly permitted fallback when no API/MCP exists and no security/CAPTCHA circumvention is required.

Dependencies must be actively maintained, appropriately licensed, and security reviewed before production promotion.

## 6. Revenue engines

Priority is economic and global:

- **P0 Revenue protection:** revisions, failed-submission repair, payment clarification, deadlines, final QA.
- **P1 Instant claim:** profitable work that can be claimed deterministically.
- **P2 Instant accept:** profitable work where a documented threshold guarantees acceptance.
- **P3 Assigned/inbound:** work already assigned to registered Amarktai agents/services.
- **P4 High-EV limited competition:** selective applications/bids.
- **P5 Profitable microjobs:** short structured tasks with low cost and strong machine verification.
- **P6 Medium jobs:** research, localisation, compliance, data, docs, coding, testing.
- **P7 Bounty upside:** higher-value coding/reward work, not treated as the daily floor.

When paid work is scarce, useful capacity performs `INVESTMENT` work such as template improvement, model benchmarking, loss analysis, market discovery, cache preparation, and reputation analysis. Investment is never counted as revenue.

## 7. Market adapters

Initial intended markets are Dealwork, Toku, Callboard, AgentGigs, TaskBounty, and Opire. Every adapter sits behind `markets.base.MarketAdapter` and advertises capability flags rather than pretending all marketplaces support the same lifecycle.

A market can only acquire work after verification of:

1. current existence and active supply;
2. official API/MCP automation support;
3. policy permission for autonomous agents;
4. legitimate payout mechanism;
5. successful South African payout onboarding;
6. Webdock compatibility;
7. viable expected economics;
8. known commission/withdrawal rules.

Failure states include `WATCH_ONLY`, `PAYOUT_BLOCKED`, `POLICY_DISABLED`, and `UNPROFITABLE`. Market count is never a KPI.

Current implementation note: the AgentGigs adapter is present because its public API documents autonomous browsing, applying, delivery, messaging, and payout workflows. Acquisition remains disabled by default until real account authentication, Stripe Connect onboarding, and South African payout readiness are proven.

## 8. Agent architecture

Logical specialists include:

- Money Brain / Global Scheduler / Policy Agent / Market Discovery / GenX Cost Controller / Reputation / Treasury / Node Manager;
- marketplace scouts;
- Structured Data, Research, Documents, Localisation, Transcription, Small Code, Heavy Code, CI/Testing, Media;
- deterministic QA, AI QA, Repair, Revision, Submission.

Logical agents are cheap; running processes are not. Workers activate only when useful work exists and resource limits permit it.

## 9. GenX architecture

GenX is the V1 AI inference gateway. The implementation uses the documented Router base URL `https://query.genx.sh` and queries live model/account data instead of hard-coding a model catalogue.

Gateway responsibilities:

- `GET /api/v1/models` for live model discovery;
- `GET /api/v1/account/credits` for wallet state;
- `GET /api/v1/account/pricing` for tier-adjusted pricing;
- `POST /api/v1/generate` and job status endpoints for asynchronous generation;
- per-request metadata for node, worker, job, market, and task class;
- per-job budget enforcement;
- actual model/usage/latency/status/cost recording;
- profitability calculations by model/task/worker/market.

The model-routing objective is accepted/settled economic return per GenX cost, not the largest model available. Deterministic local code must be preferred whenever AI is unnecessary.

## 10. Security architecture

Public exposure is limited to HTTPS and operationally restricted SSH. PostgreSQL, Redis, internal worker endpoints, GenX gateway, GitHub gateway, and sandbox-broker APIs remain private.

Production security includes UFW, SSH key-only access, disabled password SSH, fail2ban, Caddy TLS, security headers, automatic safe security updates, structured audit logs, rate limiting, secret redaction, encrypted secret storage, key rotation, and security alerts.

Job execution is untrusted. External coding/file work runs in disposable non-root containers with CPU/RAM/PID limits, no privileged mode, no Docker socket, no controller filesystem, no GenX/market/payout master credentials, isolated volumes, and restricted networking where practical.

## 11. Authentication architecture

There is no public registration and no customer-account system. The owner account is created from the server-side bootstrap command.

Authentication sequence:

1. owner username/email + strong password;
2. mandatory TOTP challenge;
3. short-lived access JWT in HttpOnly cookie;
4. rotating refresh JWT in HttpOnly cookie;
5. server-side refresh-session record and revocation state;
6. one-time recovery codes for emergency access.

Sensitive actions require fresh password + 2FA reauthentication before changing payout settings, viewing/copying secrets, rotating credentials, disabling 2FA, generating recovery codes, changing security settings, enabling a new autonomous market, or taking emergency treasury actions.

## 12. JWT design

JWTs include `iss`, `aud`, `iat`, `exp`, `jti`, token type, and a `kid` signing-key identifier. Access tokens are short lived. Refresh tokens rotate on use and belong to a server-side family; reuse of a replaced/revoked token revokes the remaining active family.

Browser JWTs are never stored in localStorage. Cookies use `HttpOnly`, `Secure`, and `SameSite=Strict` in production. CSRF protection remains enabled.

Signing keys are supplied outside Git through `JWT_SIGNING_KEYS_JSON`; `JWT_ACTIVE_KID` enables controlled rotation.

## 13. 2FA design

TOTP is mandatory for V1. The owner bootstrap flow creates an authenticator secret, encrypts it using an external Fernet key, prints the provisioning URI once, and generates one-time recovery codes stored only as password hashes.

Recovery codes are shown once, individually consumable, and can be revoked/recreated. Passkey/WebAuthn support is an upgrade path; it does not remove the required secure fallback.

## 14. Storage architecture

Persistent production storage lives under Docker volumes / `/var/lib/amarktai-earn/` equivalents for:

- PostgreSQL;
- Redis persistence;
- accepted artifacts and required evidence;
- jobs/repositories/cache;
- backups/logs/uploads.

Ephemeral workspaces, temp clones, intermediate media, tests, and disposable containers are removed under retention rules.

## 15. Database architecture

PostgreSQL is the source of truth. Initial implemented entities include owner security, refresh sessions, recovery codes, login challenges, marketplaces, market candidates, jobs, job scores, locks, workers, GenX calls, QA checks, submissions, revisions, payouts, ledger entries, and audit events.

The target schema also covers marketplace credentials/policies/health, payout accounts, applications/bids/claims/messages, execution versions, nodes, artifacts, treasury balances, alerts, model statistics, and system settings as the vertical slices are completed.

All production schema changes use migrations.

## 16. Job lifecycle

Core states are strictly separated:

`DISCOVERED -> EXPECTED -> CLAIMED/AWARDED -> EXECUTING -> SUBMITTED -> ACCEPTED/EARNED -> PAYOUT_PENDING -> SETTLED`

Failure/revision paths preserve economic history. Only `SETTLED` means cash was received.

## 17. Profit-scoring system

The initial deterministic score is based on:

`Expected Cash = (Gross Reward - Marketplace Fee) × P(Acquire) × P(Accept) × P(Payment)`

`Expected Profit = Expected Cash - GenX Cost - External Cost - Compute Cost - Opportunity Cost`

The scheduler also tracks expected profit per worker minute and expected profit per GenX cost/credit. Historical outcomes progressively update acquisition, acceptance, payment, market, task, worker, requester, and model priors.

No LLM is allowed to replace the numeric decision contract with an unstructured statement such as “looks profitable.”

## 18. Worker lifecycle

Workers publish heartbeats and move through statuses such as `READY`, `WATCHING`, `SCORING`, `CLAIMING`, `BIDDING`, `APPLYING`, `EXECUTING`, `TESTING`, `QA`, `REPAIRING`, `SUBMITTING`, `AWAITING_REVIEW`, `REVISION_REQUIRED`, `PAYOUT_PENDING`, `INVESTMENT`, and `ERROR`.

The resource governor decides how many heavy/light workers can run based on actual CPU, RAM, disk, load, active worker count, and queue length. V1 expects roughly two heavy code jobs plus several lightweight asynchronous jobs when resources permit.

## 19. QA and repair system

The creator does not approve its own output. QA may combine deterministic schema/file/test validation, reconciliation, syntax checks, citation checks, placeholder validation, second-model review, and explicit acceptance-rubric matching.

Failed QA routes to Repair with bounded retry counts. Infinite repair loops are prohibited. Already-awarded work can receive higher continuation priority when stopping would risk significantly more protected revenue.

## 20. Dashboard design

The owner dashboard is dark, professional, financial-operations focused, desktop-first responsive, and must answer within seconds: **Is Amarktai Earn actually making money?**

Navigation:

`Overview · Live Work · Agents · Markets · Earnings · Treasury · GenX · Nodes · Storage · Performance · Logs · Alerts · Settings · Security`

Overview separates actual accepted earnings, settled cash, pending payout, rolling net, active paid work, worker state, true idle, GenX usage/balance, and system health. Expected revenue is visually and semantically distinct from earned or settled revenue.

The initial bootstrap includes a secure login screen and live overview API backed by real database rows—no fake revenue seed data.

## 21. Treasury and earnings states

Treasury aggregates external market balances while preserving each market’s own payout state. Ledger events include reward recognition, marketplace fees, GenX expense, external delegation cost, payout receivable, settlement, refund/reversal, and operational expense.

Historical truth comes from ledger entries and external reconciliations, not mutable dashboard totals.

## 22. South African payout considerations

A market cannot acquire paid work until real payout onboarding succeeds for the owner/operator in South Africa. “Uses Stripe Connect” does not itself prove eligibility.

No fake foreign address, identity, company, or entity may be created to bypass payment restrictions. For future Amarktai-controlled payments, Paystack may be evaluated as a South African rail; Wise Business may be used legitimately for supported treasury/FX receiving. Neither replaces the marketplace’s own required payout onboarding.

## 23. VPS deployment

Production services:

- Caddy;
- Django web/API;
- PostgreSQL;
- Redis;
- RQ worker pools/scheduler;
- market watchers;
- Money Brain;
- GenX Gateway;
- GitHub Gateway;
- sandbox broker;
- payment watcher;
- node controller.

The initial Compose file provides isolated internal networking and persistent volumes. Production bootstrap additionally configures host firewall/SSH/fail2ban, secrets, migrations, owner creation, Caddy DNS/TLS readiness, and smoke tests.

## 24. Backup/restore

Daily minimum encrypted backups cover PostgreSQL, configuration/economic history, audit records, and required encrypted credential metadata. Disposable workspaces/repositories are not backed up unnecessarily.

`scripts/backup.sh` and `scripts/restore.sh` provide the starting encrypted database workflow. Restore must be tested before production acceptance. When meaningful revenue exists, add an offsite encrypted backup and test restore at least weekly.

## 25. Monitoring

Monitor service health, worker heartbeats, queue length, CPU, RAM, disk, load, DB/Redis availability, market auth/payout state, GenX balance/cost anomalies, retries, controller health, and backup recency.

Logs are structured JSON in production and include timestamp, node, worker, market, job, event type, severity, and correlation ID with automatic secret redaction.

## 26. Self-healing

A deterministic watchdog—not an LLM—restarts crashed services/workers, detects stuck jobs, releases expired locks, retries transient APIs with backoff, circuit-breaks broken markets, bounds repair loops, cleans abandoned containers/workspaces, pauses heavy work under resource pressure, and resumes safely after reboot.

One broken marketplace credential must never stop the system.

## 27. Multi-VPS scaling

All nodes run the same versioned Amarktai Earn release. VPS1 remains the logical hub. Secondary nodes enroll with one-time tokens and mTLS, receive a constrained role profile, heartbeat, and become schedulable.

Scale only when measured positive economics and capacity pressure justify the incremental VPS cost. The future dashboard should calculate lost positive expected value due to capacity.

## 28. Versioning/upgrades

Release images are versioned (`amarktai-earn:1.x.y`). Upgrade flow is build -> tests -> canary -> one secondary node -> verify -> rolling fleet -> automatic rollback on failed health.

Nodes report release version and health. New nodes always join using the current tested release rather than old copies.

## 29. Two-day V1 build plan

**Day 1:** repository/README, Compose, Django/PostgreSQL/Redis/Caddy, owner auth, database/economic schema, GenX gateway, market interface + first verified adapter, initial dashboard.
**Day 2:** deterministic data/research/document/localisation workers, Aider/OpenHands coding workers, QA/repair/revision/submission, payout/Treasury, remaining viable market watchers, watchdog/resource/storage governance, shadow-mode run, then conservative low-risk autonomous acquisition.

Vertical slices take precedence over six half-built integrations.

## 30. 14-day optimisation plan

Days 1–3: prove a real autonomous acquisition and paid completion; collect GenX and marketplace friction data.
Days 4–7: improve instant-claim latency, deterministic microtask templates, QA, bid thresholds, and model economics.
Days 8–14: reallocate toward proven task/market/model lanes. If rolling economics are poor, redesign the economics rather than adding servers.

## 31. 30/60/90-day growth plan

**30 days:** multiple proven task classes, robust payout reconciliation, stable worker QA, cost-aware GenX routing, measurable market statistics.
**60 days:** add a second node only if profitable queue pressure and positive incremental economics are demonstrated; add specialized worker profiles and automated capacity recommendations.
**90 days:** diversify profitable market/task lanes, strengthen controller failover, expand paid QA/review opportunities, and approach cluster targets only when settled evidence supports them.

## 32. Long-term upgrade plan

Phase A: one VPS proves economics.
Phase B: improve profitable task classes and reputation.
Phase C: add VPS2 after queue/capacity proof.
Phase D: specialize fleet roles.
Phase E: five-node cluster when economically justified.
Phase F: improve marketplace portfolio and paid QA/review.
Phase G: controller failover/high availability.
Phase H: automated capacity recommendations and safe versioned worker-factory proposals.

Production self-modification is never uncontrolled; proposed worker changes are versioned, tested, evaluated, and promoted only after proof.

## 33. Explicit prohibited features

Do not build crypto/mining/DePIN, bandwidth resale, fake traffic, spam, survey fraud, ad-click bots, fake marketplace identities, multi-account manipulation, trading bots, fake earnings, uncontrolled browser automation, local prohibited model inference, public databases/Redis, JWT localStorage, secrets in Git, worker access to master credentials, duplicate global bidding, or production claims without end-to-end proof.

## 34. Production acceptance criteria

V1 is operational only when:

1. `earn.amarktai.co.za` serves valid HTTPS;
2. owner-only login works;
3. JWT access/rotation/replay protection works;
4. TOTP is mandatory and recovery codes are one-time;
5. PostgreSQL/Redis state survives restart;
6. storage thresholds/cleanup are proven;
7. GenX live model/pricing/credits/generation work and per-job usage is recorded;
8. at least one currently viable, payout-ready market is fully integrated;
9. real opportunities are discovered and normalized;
10. policy/economic gates and global locks prevent duplicate acquisition;
11. a real worker executes a real task;
12. independent QA and bounded repair run;
13. submission/revision/payment-state tracking works;
14. dashboard reflects real records only;
15. reboot/watchdog recovery works;
16. job sandboxes cannot access master secrets;
17. prohibited Webdock workloads are absent;
18. this README matches the implementation.

Best economic proof: one real funded job acquired, completed, independently QA’d, submitted, and tracked without routine operator intervention.

## 35. Required marketplace/API onboarding checklist

Before enabling acquisition for each market:

- create/verify the legitimate account;
- complete required KYC manually;
- confirm API/MCP authentication;
- verify profile/service readiness;
- verify job discovery;
- verify autonomous-agent policy;
- verify South African payout eligibility by actual onboarding;
- verify payout destination and withdrawal method;
- record commission/fees;
- test webhook/messages where supported;
- mark `payout_ready=true` only after proof.

Infrastructure credentials required outside Git include GenX, marketplace keys, JWT signing keys, field-encryption keys, PostgreSQL password, backup passphrase, and any GitHub gateway credential. Missing credentials block only the dependent live integration; they do not stop independent build work.

## Repository structure

```text
Amarktai-Earn/
├── README.md
├── docker-compose.yml
├── Dockerfile
├── Caddyfile
├── .env.example
├── control/          # Django control plane, auth, economics, ledger state
├── markets/          # common market adapter interface + verified adapters
├── gateways/         # GenX/GitHub/sandbox gateway implementations
├── workers/          # deterministic and AI-assisted worker classes
├── docs/             # supporting implementation notes only
├── tests/            # unit/integration/economics/sandbox/e2e
└── scripts/          # bootstrap/deploy/backup/restore/smoke tooling
```

## Current build state

The bootstrap establishes the real application skeleton, secure owner-auth primitives, database/economic models, persistent Compose topology, encrypted field-secret primitive, GenX Router client, AgentGigs read-only/discovery+apply+submit adapter skeleton, initial owner login/overview UI, backup/restore scripts, and economics tests. It does **not** claim live earnings or a live payout-ready market until credentials/KYC/payout proof exists.

The next implementation slices are: migrations and auth tests -> GenX persistence/budget enforcement -> global lock acquisition API -> worker execution/QA pipeline -> first payout-ready live market vertical -> full operations dashboard -> watchdog/resource/storage governors.
