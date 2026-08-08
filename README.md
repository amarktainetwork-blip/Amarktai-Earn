# Amarktai Earn

**Autonomous Digital Income Operating System**
Production domain: `https://earn.amarktai.co.za`
Repository: `Amarktai-Earn`
Primary deployment: one Webdock VPS, expandable into one centrally controlled multi-node fleet.

This README is the primary source of truth. Supporting documents may explain implementation details, but they may not redefine the product, revenue model, security boundaries, money states, or deployment architecture described here.

### Current verified implementation status — 2026-08-08

The repository has moved beyond the bootstrap/controller-only stage but is **not yet production-operational or earning-capable**. The verified code foundation now includes Docker/Caddy/PostgreSQL/Redis configuration, Django owner control plane, JWT/TOTP/recovery-code authentication code, additive database migrations, deterministic economic scoring and acquisition gates, transactional global job leases with fencing tokens, expanded market/execution/treasury/runtime entities, one AgentGigs adapter in payout-blocked mode, a controller-owned GenX catalog/budget/reconciliation gateway, one real structured-data execution path with persisted artifacts and independent deterministic CSV QA, append-only payout/ledger state handling, encrypted backup/restore scripts, and an initial truth-only operations dashboard.

Deterministic validation in the build environment currently passes 25/25 tests plus syntax and whitespace checks. Django/PostgreSQL/Redis integration and live GenX calls still require a runtime with the production dependencies/credentials, so none of those are falsely marked live-proven. Live marketplace payout onboarding, a fully payout-ready market vertical, repair/submission/revision loops, broader worker coverage, sandbox execution, scheduler/watchers, resource/storage governors, watchdog recovery, and the first real end-to-end paid completion are still required.


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

Only `SETTLED` is received cash. `EXPECTED`, `CLAIMED`, `AWARDED`, `SUBMITTED`, `ACCEPTED/EARNED`, and `PAYOUT_PENDING` are separate economic states and must never be displayed as settled revenue.

## 3. Architecture

VPS1 is the permanent logical hub and owns the dashboard, PostgreSQL, Redis, scheduler, Money Brain, marketplace gateways, GenX gateway, GitHub gateway, Treasury, global job locks, node registry, central reporting, and security control plane.

External work is normalized into one internal job contract. The control plane makes deterministic economic/policy decisions before workers are allowed to spend GenX credits or external resources.

Logical flow:

`Market adapters -> Normalizer -> Policy + payout gate -> Economic scorer -> Global lease -> Scheduler -> Worker -> Independent QA -> Repair if needed -> Submission -> Revision watcher -> Payout watcher -> Ledger -> Learning`

The first node may expose many logical agents while running only the workers that currently have work. Additional VPSs join as worker nodes; they do not become independent competing brains.

## 4. Webdock restrictions

The following are excluded from this repository and deployment:

- cryptocurrency mining, validators, nodes, staking or testnets;
- DePIN/decentralised compute or storage;
- bandwidth resale, packet sharing, proxies or traffic exchanges;
- Tor/torrenting;
- network scanning, unauthorised security testing, DDoS/stress testing;
- sustained heavy third-party scraping;
- prohibited local neural-network inference/training;
- spam, unsolicited bulk messaging or ad-click automation;
- survey manipulation, fake marketplace accounts/traffic, deceptive affiliate systems;
- any workload that threatens the hosting account.

A future incompatible business belongs in a different repository on a host that explicitly permits it. No dormant crypto/DePIN modules are shipped here.

## 5. Technology stack

- Python 3.12+
- Django 5.2
- PostgreSQL
- Redis + RQ
- Caddy
- Docker / Docker Compose
- Pydantic / PydanticAI where structured agent decisions add value
- official MCP Python SDK for MCP marketplaces
- Aider for smaller repository work
- OpenHands SDK for heavier repository work
- pandas / Polars / DuckDB / openpyxl for structured-data jobs
- FFmpeg for profitable permitted media manipulation
- Playwright/Browser Use only as an explicitly permitted fallback
- GenX Router for remote AI inference

Dependencies must be maintained, appropriately licensed, security reviewed, and economically justified.

## 6. Revenue engines

Priority is economic, not cosmetic:

- **P0 Revenue protection:** revisions, maintainer/requester feedback, submission repair, payout clarification, deadlines, final QA.
- **P1 Instant claim:** claim profitable work immediately where a marketplace explicitly allows it.
- **P2 Instant accept:** profitable work where a documented threshold guarantees acceptance.
- **P3 Assigned/inbound:** already-awarded work or service orders.
- **P4 High-value low-competition:** selective applications/bids.
- **P5 Profitable microjobs:** small structured, fast, machine-verifiable work with low AI cost.
- **P6 Medium jobs:** research, localisation, compliance, data, documentation, coding/testing.
- **P7 Bounties:** upside rather than the daily floor.

Quiet time becomes `INVESTMENT`, never fake revenue: market discovery, reusable templates, dependency/repo caching, model benchmarking, loss analysis, skill improvement, test-template work, and stale-workspace cleanup.

## 7. Market adapters

Every marketplace lives behind a common interface with capability flags for health, payout readiness, discovery, normalize, claim, bid, apply, messages, submission, status, and payout.

Initial candidates from the governing specification:

- Dealwork
- Toku
- Callboard
- AgentGigs
- TaskBounty
- Opire

None is enabled merely because its name is in this list. Before autonomous acquisition, the current official policy/API/MCP, actual supply, account auth, payout flow, South African eligibility, expected economics and Webdock compatibility must be verified.

Current implementation note: the AgentGigs adapter is present because its public API documents autonomous browsing, applying, delivery, messaging, and payout workflows. Acquisition remains disabled by default until real account authentication, Stripe Connect onboarding, and South African payout readiness are proven.

## 8. Agent architecture

Core logical roles:

- Money Brain / Global Scheduler;
- Market Discovery / Policy / GenX Cost Controller;
- Treasury / Reputation / Node Manager;
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
- persisted model/pricing catalog and account-credit snapshots;
- per-request metadata for node, worker, job, market, task class, requested model and maximum credits;
- explicit call-level and job-level credit reservation before remote submission;
- request-key replay protection; ambiguous network/timeout outcomes remain `UNKNOWN_REMOTE_STATE` until reconciled;
- actual model/usage/latency/status/result recording when GenX returns it;
- model routing using historical profit-per-credit/acceptance before weak price hints; known loss-making history does not outrank unproven alternatives;
- profitability statistics by model/task, with wider worker/market attribution added as settled outcomes accumulate.

`python manage.py sync_genx_catalog` refreshes the live catalog/pricing/credit snapshot. `python manage.py reconcile_genx` polls already-submitted remote job IDs and never replays an uncertain request merely because the controller lost the response.

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

PostgreSQL is the source of truth. Implemented migration-backed entities now include owner security, refresh sessions, recovery codes, login challenges, marketplaces, encrypted marketplace credentials, market policy versions and health snapshots, payout accounts, market candidates, jobs, job scores, locks, applications, bids, claims, job messages, nodes, workers and worker versions, executions, artifacts, GenX model catalog/account snapshots/calls/model statistics, QA checks, submissions, revisions, payouts, ledger entries, treasury balances, alerts, system settings, and audit events.

The runtime keeps the original normalized marketplace payload on each job and all material acquisition/execution/payment state changes are persisted rather than inferred from logs. Further tables may be added only when a real vertical requires them; schema-by-hand production changes are prohibited.

All production schema changes use migrations. `0001` remains immutable history and later capabilities are added through `0002+` migrations.

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

Every market stores explicit payout readiness and South African verification flags. A marketplace can remain `WATCH_ONLY` while discovery works, but autonomous acquisition remains blocked until payout onboarding succeeds.

Never invent foreign addresses/entities or bypass payment-platform restrictions. Marketplace payout rails are dictated by each platform; Amarktai cannot replace them from its own code. Paystack/Wise may be used later for Amarktai-controlled treasury flows where legally suitable, but do not substitute for required marketplace onboarding.

## 23. VPS deployment

Initial target is VPS1:

- 8 vCPU;
- 10 GB RAM;
- 100 GB disk;
- 1 Gbit networking.

Core containers/services: Caddy, Django/Gunicorn, PostgreSQL, Redis/RQ, controller/watchers, worker pool. Heavy untrusted execution later runs only through the sandbox broker.

Production deployment keeps only required ports public and uses persistent volumes for database/Redis and `/var/lib/amarktai-earn/*` data classes.

## 24. Backup/restore

`scripts/backup.sh` and `scripts/restore.sh` provide the starting encrypted database workflow. Restore must be tested before production acceptance. When meaningful revenue exists, add an offsite encrypted backup and test restore at least weekly.

Backups never go into Git. Retention prevents backup files filling the disk.

## 25. Monitoring

Monitor controller/API/worker/market/GenX health, queue depth, job duration, acceptance, payout aging, CPU, RAM, disk, error rates, auth/security events, and true idle. Alert on economics/security/availability events that require intervention.

Health must distinguish a single broken marketplace from overall system failure.

## 26. Self-healing

A deterministic watchdog, independent of AI availability, will restart crashed workers/services, release expired leases, detect stuck jobs, back off rate limits, circuit-break failing markets, prevent infinite repair loops, remove abandoned containers/workspaces, pause heavy jobs under resource pressure and resume cleanly after reboot.

A marketplace auth failure must isolate that market instead of stopping the rest of Amarktai Earn.

## 27. Multi-VPS scaling

The database lease/fencing-token mechanism exists from V1 so later VPSs cannot bid/claim/execute the same opportunity accidentally. VPS1 remains the central control/economic authority; workers communicate through authenticated internal gateways and queues.

Node records include hostname, release version, role, health, resource telemetry and heartbeat. New nodes start in worker roles and receive no master marketplace/GenX/payout credentials.

## 28. Versioning/upgrades

Every worker/controller release is versioned. Deploy migrations before dependent code, retain rollback/recovery capability, expose version/health on nodes, and progressively roll worker versions rather than updating an entire future cluster blindly.

Third-party dependencies are pinned by compatible ranges initially and will gain lockfiles/image digest pinning before production acceptance.

## 29. Two-day V1 build plan

**Day 1:** repository/README, Compose, Django/PostgreSQL/Redis/Caddy, owner auth, database/economic schema, GenX gateway, market interface + first verified adapter, initial dashboard.

**Day 2:** complete first real market vertical, worker execution + QA/repair + submission, scheduler/watchers, payout reconciliation/ledger, sandbox broker, storage/resource governor, watchdog, end-to-end smoke tests, VPS TLS deployment and shadow-mode verification.

The two-day plan is an execution target, not permission to skip production acceptance criteria.

## 30. 14-day optimisation plan

1. collect real acquisition/acceptance/payment/model data;
2. optimize thresholds per task/market;
3. expand deterministic worker templates;
4. add the next payout-ready market;
5. improve model routing from real revenue/credit data;
6. tune concurrency and caches;
7. reduce artifact/log storage;
8. improve revision response and reputation;
9. add verified market discovery candidates;
10. introduce paid QA/review only where genuinely available.

## 31. 30/60/90-day growth plan

**30 days:** stabilize the most profitable task/market pairs, target rolling $20–$30/day, eliminate unprofitable categories.

**60 days:** expand profitable service/inbound lanes, mature worker templates and model statistics, approach $50/day if external supply supports it.

**90 days:** add nodes only where capacity is a proven bottleneck, expand verified markets, target $100–$133/day cluster-wide when actual demand/economics justify it.

Targets are never transformed into fake forecasts or displayed as settled revenue.

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

The current `main` target now contains the controller foundation plus the next runtime layer: migration-backed economic/runtime entities, global acquisition leases, persistent acquisition attempts that stop blind replay after uncertain remote outcomes, GenX live-catalog/pricing/credit synchronization code, per-job GenX credit reservations and request-key replay protection, GenX reconciliation, a structured-data `acquired -> execute -> artifact -> deterministic QA` path, payout state transitions, idempotent ledger postings, treasury recomputation, and overview metrics sourced only from database truth. It does **not** claim live earnings or a live payout-ready market until credentials/KYC/payout proof exists.

The immediate critical path is now: prove Django/PostgreSQL/Redis migrations and owner auth on the VPS -> configure/live-test GenX catalog + one budgeted call -> choose and complete the first payout-ready market adapter -> wire `discover -> score -> acquire -> execute -> QA -> submit -> revision -> payout` -> add bounded repair -> add RQ scheduler/watchers -> sandbox untrusted jobs -> finish watchdog/resource/storage governors and dashboard surfaces -> prove one real settled paid completion.
