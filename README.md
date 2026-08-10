# Amarktai Earn

Amarktai Earn is a private, owner-only controller for legitimate paid digital work. Its objective is to maximize sustainable **net settled profit and reputation growth using safely available productive capacity**. PostgreSQL is the economic and operational source of truth; Redis supports coordination; every acquisition, execution, submission, and money transition is fail-closed.

Expanded V1 is **CODE-COMPLETE** in this repository. **CI-PROVEN** applies only to a commit whose complete sequential GitHub Actions gate is green. Neither term means the system is deployed, externally approved, payout-ready, or earning. Those facts remain `EXTERNAL_PROOF_REQUIRED` until production evidence exists.

## Truth vocabulary and money states

- **CODE-COMPLETE** — the reviewed implementation exists in the repository.
- **CI-PROVEN** — deterministic, PostgreSQL/Redis, production-image, isolation, secret-exclusion, and backup/restore gates passed in one sequential run.
- **SOURCE-WIRED** — a real external integration path exists; live availability is not implied.
- **RUNTIME-PROVEN** — the deployed production environment exercised a path successfully.
- **PAYOUT-PROVEN** — a real marketplace payout was reconciled against its external rail.
- **SETTLED** — cash was actually received and reconciled.

Job truth is `DISCOVERED → EXPECTED → CLAIMED/AWARDED → EXECUTING → SUBMITTED → ACCEPTED → PAYOUT_PENDING → SETTLED`, with `FAILED` explicit. Payout truth is `EARNED`, `PAYOUT_PENDING`, `SETTLED`, or `REVERSED`.

Only `SETTLED` is received cash. Rewards, exposure, expected profit, applications, awards, accepted work, and payout-pending amounts are not revenue. `TARGET_*` values are objective floors, never achieved-revenue claims or earnings caps. Exceeding a target never stops discovery, acquisition, execution, or additional profitable work.

## Architecture and lifecycle

```text
market/source discovery
  → normalized opportunity and exact capability
  → workload + autonomy + policy + payout + economic + resource gates
  → lease and immutable input manifest
  → WorkPlan or composite DAG
  → registered worker(s)
  → persisted artifacts
  → independent operation-specific QA
  → bounded repair when eligible
  → submission and revision protection
  → acceptance and payout reconciliation
  → SETTLED ledger truth
  → performance, reputation, pricing, and capacity learning
```

Markets are adapters; workers are replaceable capabilities. Interfaces never choose GenX models or providers directly. The controller routes work by capability, policy, quality, economics, capacity, history, and risk. Already-awarded work has priority over speculative work, especially during revisions, deadline risk, delivery defects, payout issues, and disputes.

The controller never silently self-modifies production source. Proposed code improvements must pass through a constrained coding sandbox, patch review, tests, independent QA, a branch/PR, CI, and explicit merge rules. There is no autonomous direct-to-main path.

GenX session message responses are submission acknowledgements, not generation results. The controller persists the session, message, and remote-job identities separately, keeps the call `SUBMITTED`, polls only the returned remote job, and finalizes the model attempt only after a terminal job payload. A timeout or ambiguous delivery becomes `UNKNOWN_REMOTE_STATE` and is reconciled without replaying the paid POST. Missing usage remains `billing_truth=UNRESOLVED`, keeps the estimated credit reservation in force, and produces one deduplicated alert until authoritative poll, authenticated future webhook, or operator evidence supplies actual usage. No unauthenticated GenX webhook endpoint is exposed.

## Profit Brain and Growth Governor

The Profit Brain persists decisions and outcomes rather than treating environment variables as economic memory:

- `GrowthTarget` and `GrowthEvaluation` track daily/weekly net-settled-profit objectives, completed jobs, QA, revisions, GenX cost ratio, active markets, and profitable capabilities. Growth stage measures evidence maturity; BOOTSTRAP never means low earnings are required.
- Status is `AHEAD`, `ON_TRACK`, `BEHIND`, or `INSUFFICIENT_DATA`, with reason codes such as low award rate, capacity idle/saturated, payout blocked, high GenX cost, revision/worker degradation, auth failure, and queue congestion.
- `PerformanceAggregate` maintains rolling market, capability, operation, worker/version, market-capability, and pricing views. It records attempts, awards, completion, QA/repair/revision/on-time/acceptance/settlement rates, settled payout and costs, execution and settlement latency, profit per minute/GenX credit, and observed reputation change.
- Growth stages are `BOOTSTRAP`, `ESTABLISH`, `PROFIT`, and `SCALE`. Conservative sample requirements prevent one lucky bounty from becoming a strategy.
- `OpportunityDecision`, `PricingStrategy`, and `StrategyAdjustment` preserve why work or pricing was selected. Learning is bounded; strategy changes are auditable.

Recorded net settled profit is calculated as USD `Payout.net` for payouts settled in the measurement window minus every finalized, persisted `GenXCall.cost_equivalent` attributable to those settled jobs and known by the reporting cutoff. Cost attribution follows the job whose payout settled, so valid execution cost incurred before the settlement window is still deducted; evidence created or finalized at or after a supplied historical window end is excluded. `Payout.net` already excludes the recorded marketplace fee, so the fee is not subtracted twice. GenX usage is denominated in credits and this repository has no authoritative persisted credits-to-USD conversion; missing monetary cost is therefore marked as incomplete coverage and is never fabricated as zero or presented as final “true profit.” Growth evaluation treats incomplete settled-profit coverage as insufficient data. The repository also has no persisted actual external/direct-cost source; dashboards and aggregate evidence state that limitation explicitly rather than substituting estimates.

Paid-resource approval is profitability-relative. The normal per-job envelope scales with persisted gross reward, marketplace fee, expected GenX cost, expected external cost, known operational cost, expected cash profit, risk-adjusted profit, execution time, opportunity cost, utilization, and available GenX budget. Positive cash and risk-adjusted profit remain mandatory. `ABSOLUTE_MAX_PAID_COST_PER_JOB_USD` is a finite emergency runaway-spend circuit breaker, not a desired spend, growth-stage limit, or profit target. Fresh-clone paid execution remains disabled by the existing autonomy, market, and zero GenX switches.

### Utilization, micro-profit, opportunity cost, and reputation

`CapacitySnapshot` records productive, active, available, and reserved slots; productive utilization; eligible waiting work; avoidable/unavoidable idle; foregone expected profit; and the reservation reason.

When capacity is genuinely idle, positive micro-profit work can pass a lower reviewed profit-per-minute threshold so the system does not reject executable profit arbitrarily. When capacity is constrained, higher expected risk-adjusted profit per scarce execution minute wins and existing obligations remain protected or queued by the existing lifecycle. Resource concurrency limits protect the VPS; they are not growth-stage or revenue limits. Resource, deadline, policy, payout, capability, and safety gates are never relaxed merely to avoid idleness.

Pricing uses total expected cost, platform fee, stage, utilization, advertised budget, competitive information, and adequate historical win-rate samples. It never offers below the calculated minimum profitable price. Exploration is capacity-bounded. Loss-making reputation investment is disabled by default, limited to bootstrap conditions, requires a positive recorded reputation signal, and has a separately configured daily loss budget.

`ReputationSnapshot` stores only signals the market actually exposes: source, rating/count, completions, revision rate, on-time rate, capability, and observation time. Missing ratings are not invented.

## Registered worker catalogue

Every class below is registry-backed, has explicit operations, runs through the common execution/artifact lifecycle, and has independent deterministic QA. Production enablement is separate from runtime status and can remain blocked by GenX, sandbox, policy, credential, resource, or feature gates.

| Worker class | V1 work | QA/runtime truth |
|---|---|---|
| `structured_data` | JSON/CSV conversion and normalization | reopened deterministic CSV |
| `documents` | extract, summarize, rewrite PDF/DOCX/TXT/Markdown | document QA; bounded GenX where required |
| `research` | cited research reports | citation/source QA; budgeted GenX |
| `localization` | explicit target-language translation | structural translation QA; budgeted GenX |
| `transcription` | bounded audio/video transcription | transcript QA; bounded upload/GenX lifecycle |
| `code_small` | scoped Aider repository changes | isolated patch/test QA |
| `code_heavy` | scoped OpenHands repository changes | isolated patch/test QA |
| `ci_testing` | repository test execution | isolated test-result QA |
| `media` | resize/crop/convert/compress, trim/transcode, audio extraction | decoded format/dimension/duration/stream QA |
| `advanced_structured_data` | tabular convert/normalize/deduplicate/merge/filter/map/schema validate | spreadsheet/CSV/JSON reopen QA |
| `spreadsheet_reporting` | professional XLSX reports | workbook reopen/formula-safety QA |
| `data_analysis` | deterministic descriptive analysis | structured analysis QA |
| `technical_documentation` | repository-scoped technical docs | document/source QA |
| `content_copy` | bounded content and copy deliverables | professional text QA; no external publishing claim |
| `seo_audit` | supplied-site SEO audit | structured audit QA |
| `presentations` | presentation artifacts | slide/deck structure QA |
| `document_production` | professional document artifacts | document reopen QA |
| `public_web_data` | explicitly allowed public-web extraction | provenance/shape QA; feature-disabled by default |
| `web_output` | bounded static HTML output | parsed static-page QA |
| `defensive_code_review` | authorized repository-only defensive review | finding/source QA; explicit scope required |
| `customer_support` | supplied support-response deliverables | professional text QA; no external send claim |
| `synthetic_data` | commissioned schema-driven synthetic datasets | privacy/provenance/dedup/split/card reopen QA |
| `ai_safety_research` | authorized supplied/local-fixture behavior evaluations | authorization/bounds/sanitization QA; disabled by default |

## Multi-file jobs and composite workflows

Multi-file ingestion enforces configured file-count, per-file/total size, path, MIME/extension, archive, active-content, and ownership boundaries. Each accepted asset is hashed, deduplicated, assigned a semantic role, and recorded in an immutable manifest. Cross-job paths and unregistered inputs are rejected.

A composite request becomes a persisted DAG of `WorkPlanStep` records and dependencies. The planner validates step count, identifiers, cycles, worker/operation ownership, input roles, and required prior outputs. Each step runs the normal registered-worker lifecycle with its own artifacts and QA; downstream steps cannot start until dependencies pass. Repair is bounded and watchdog recovery preserves the DAG state.

## GenX architecture

GenX is a controller gateway, not a hard-coded model list. The live catalog must be synchronized and model selection is capability-, quality-, budget-, and policy-aware. Calls use idempotency keys, estimated and maximum credits, persisted latency/outcome/cost, and explicit unknown-remote reconciliation. No hard-coded GenX model ID is required for V1 routing.

GenX spending is disabled by default. CI uses deterministic substitutes and does not prove a live credential, catalog, call, model availability, or credit charge.

## Synthetic Data Factory

The factory is commissioned-first. A job must supply confirmed rights, provenance, a supported schema, bounded generation plan, and an authorized generation budget. It can generate string/integer/number/boolean fields with controlled choices, sequences, boolean cycles, and templates, or validate supplied records.

Rows pass exact schema/type/enum validation, PII-like pattern rejection, protected-source contamination checks, and exact deduplication. Accepted records receive deterministic train/validation/test splits. Deliverables include JSONL, CSV, a dataset card, and a manifest with schema, provenance, generation plan, class/split distributions, rejection counts, generation cost, GenX credits, and cost per accepted record. Independent QA reopens the dataset and card.

Speculative inventory is disabled by default and additionally requires recorded demand evidence and explicit budget authorization. The factory does not create uncommissioned inventory merely because capacity is idle.

## Authorized AI-safety research lane

The V1 lane is intentionally narrow and disabled by default. It supports only owner-supplied sandboxes/local fixtures; it performs no remote target interaction.

Before any test, the controller requires an active registered `BountyProgram`, a current hashed `ProgramScopeVersion`, an exact active `AuthorizedTarget`, an allowed test type, known rate/request/spend bounds, automation permission, current authorization, resource admission, and the feature switch. The same conditions are rechecked immediately before execution. **NO SCOPE = NO TESTING.**

Internet-wide/random scanning, credential attacks, DDoS/stress, persistence, malware, phishing, destructive exploitation, and scope bypass are prohibited. Raw prompts, harmful detail, and private target data are excluded from the report. Candidate findings require independent reproduction and a duplicate check before a submission can even become `DRAFT`. No award, payout, or settlement is inferred. Reproduced findings may become sanitized evaluation cases only after rights and privacy/harm-removal checks.

## Market architecture and exact adapter truth

The repository-level [two-sided revenue engine](docs/two-sided-revenue-engine.md) keeps demand-pull jobs and supply-push service orders on the same canonical economic, execution, QA, delivery, and settlement path. All new channels are fail-closed by default and require external account/payout proof before activation.

All markets are disabled for autonomous acquisition by default. A source or adapter is not payout proof. The database stores source wiring, capabilities, checked documentation time, auth method, rate-limit knowledge, policy status, payout method, South African eligibility, and exact blockers.

| Market | Source/adapter | Discovery | Apply/bid/claim | Submission/status | Payout truth | Current V1 truth |
|---|---|---|---|---|---|---|
| AgentGigs | REST/webhook service | source-wired | application/acquisition path exists behind policy, autonomy, payout, and separate switches | submission, revision, webhook/status lifecycle | requires real account/KYC/rail reconciliation | strongest source-wired adapter; live runtime and payout remain external proof |
| Dealwork | official REST adapter | source-wired REST discovery | REST mutation capabilities remain policy/payout/autonomy gated and disabled | REST status/mutation surface only where exposed | not verified for South Africa | runtime-proven discovery; current inventory is qualified fail-closed and payout blockers remain explicit |
| Callboard | REST adapter | discovery contract | capability-dependent and disabled | contract/status surface only where exposed | not verified for South Africa | source-wired contract; live docs/auth/policy/payout remain external proof |
| TaskBounty | REST/MCP adapter | discovery/status contract | disabled | status surface where exposed | not verified for South Africa | source-wired contract; live integration and payout proof remain external |
| Opire | public/source import adapter | source import | no autonomous mutation in V1 | source/status import only | prohibited crypto-only rails cannot open payout gate | watch/import truth; payout/policy proof required before enablement |
| Algora | public/source import adapter | source import | no autonomous mutation in V1 | source/status import only | prohibited crypto-only rails cannot open payout gate | watch/import truth; payout/policy proof required before enablement |

Contra, RapidAPI, Apify Store, and Lemon Squeezy Direct are priority **shadow-preparation** channels. Their package/pricing/placement plans may be prepared locally, but account onboarding, KYC, listing publication, checkout activation, paid external execution, and payout-route activation remain manual and fail-closed until separately proven. Scraper-heavy Apify work must execute on Apify infrastructure rather than as continuous high-load scraping on Webdock.

Deterministic CI mocks prove adapter contracts without making live-market claims. `payout_ready` requires an active non-crypto payout account with South African eligibility evidence. Cryptocurrency, mining, DePIN, bandwidth, and proxy earning routes cannot satisfy the payout gate.

## Dashboard truth

The owner-only dashboard is database-backed and exposes overview, live work, agents, markets, Banking/payment rails and marketplace-to-owner settlement routes, earnings, treasury, GenX, nodes, storage, performance, logs, alerts, settings, and security.

- Overview separates settled today/7d/30d, payout pending, awarded/accepted exposure, allowed expected profit, recorded GenX cost, target status, productive utilization, avoidable idle, active work, and blocked profitable opportunities.
- Performance shows persisted growth targets/evaluations, stage, market/capability/operation/worker/strategy profitability, QA/revision/settlement latency, capacity state, foregone expected profit, GenX model outcomes, and actual market-provided reputation observations.
- Markets show source wiring, adapter capabilities, auth/policy/rate-limit/payout/South Africa truth, discovery, applications, awards, settlements, settled net cash, and blockers.
- Agents includes every registered worker and separates production enablement reasons from live runtime state.
- Alerts combine persisted alerts with database-derived avoidable idle, behind-target, payout/auth/bid/pricing/QA/revision/synthetic/safety-scope/resource warnings.

Secrets are never returned. Only `CONFIGURED — HIDDEN` or `NOT CONFIGURED` is displayed for sensitive configuration.

## Security, sandbox, resource governor, and recovery

Security includes Argon2id owner passwords, confirmed TOTP, one-time recovery codes, short-lived JWT access, rotating refresh sessions, replay-family revocation, persistent throttles/cooldowns, password-plus-TOTP reauthentication grants, encrypted external credentials, audit events, and owner-only APIs.

Coding agents run non-root in disposable, read-only, network-constrained, CPU/memory/PID/time-bounded containers. They never receive the Docker socket, controller filesystem, or controller master secrets. A trusted deterministic broker alone controls containers. Dependency preparation accepts recognized verified lock manifests, disables unsafe hooks/scripts, builds bounded caches in isolated fetch containers, and mounts results read-only.

Resource admission covers disk and storage-class quotas, memory headroom, load, queues, code sandboxes, GenX jobs, media processes, per-market concurrency, and active obligations. Watchdog recovery handles stale heartbeats, leases, work plans/steps, executions, queue ownership, sandboxes, temporary files, and defined unknown-remote reconciliation. Ambiguous external money or mutation operations are never blindly replayed.

Retention preserves ledgers, payouts, audits, mutation records, disputed source material, accepted artifacts, and settlement evidence.

## Safe defaults

A fresh clone cannot spend credits, acquire work, test safety targets, create speculative synthetic inventory, or launch coding/dependency jobs without explicit configuration:

```dotenv
AMARKTAI_ENV=development
AUTONOMOUS_MODE=OFF
AGENTGIGS_AUTO_APPLY_ENABLED=0
AGENTGIGS_AUTO_ACQUIRE_ENABLED=0
DEALWORK_AUTO_ACQUIRE_ENABLED=0
CALLBOARD_AUTO_ACQUIRE_ENABLED=0
TASKBOUNTY_AUTO_ACQUIRE_ENABLED=0
OPIRE_AUTO_ACQUIRE_ENABLED=0
ALGORA_AUTO_ACQUIRE_ENABLED=0
SANDBOX_CODING_ENABLED=0
DEPENDENCY_PREPARATION_ENABLED=0
AGENTGIGS_MAX_GENX_CREDITS=0
SYNTHETIC_SPECULATIVE_INVENTORY_ENABLED=0
SAFETY_BOUNTY_EXECUTION_ENABLED=0
SAFETY_BOUNTY_MAX_SPEND_PER_ATTEMPT=0
REPUTATION_INVESTMENT_ENABLED=0
REPUTATION_INVESTMENT_DAILY_LIMIT=0
```

`FULL` autonomy never bypasses legality, marketplace policy, payout, capability, profitability, budget, resource, credential, sandbox, provenance, or safety scope gates.

## Acceptance and CI

```bash
python manage.py v1_acceptance --format text
python manage.py v1_acceptance --format json
```

Statuses are `PASS`, `FAIL`, `BLOCKED`, and `EXTERNAL_PROOF_REQUIRED`. The command exits non-zero on `FAIL` by default. `--ci-proven` is reserved for the final sequential GitHub Actions step after compilation, Django checks, migration drift/migrations, real PostgreSQL/Redis health, deterministic and integration suites, production preflight, Compose parsing, sandbox builds/isolation, production-image secret exclusion, and encrypted backup/clean restore.

Expanded acceptance explicitly covers Growth and Utilization governors, stages and targets, micro-profit/opportunity-cost logic, bounded pricing/exploration/reputation, multi-file/DAG execution, the full worker/QA catalogue, synthetic data, authorized safety research, multi-market/payout fail-closed contracts, dashboard/secret truth, money states, and every pre-existing core proof. External criteria can never be promoted by CI.

## Deployment architecture and multi-node future

Compose defines PostgreSQL, Redis, web/controller, workers, scheduler/watchdog, GenX gateway, trusted sandbox broker, Caddy, persistent volumes, health-aware ordering, migrations/preflight, non-root services, and restart policies. `scripts/backup.sh` and `scripts/restore.sh` provide encrypted PostgreSQL backup/restore.

Production is deployed at `https://earn.amarktai.co.za`. Runtime proof is release-specific: only a production SHA actually deployed and exercised may be called runtime-proven. External account/KYC, marketplace payout, owner payment-rail readiness, irreversible acquisition, and real settled-cash proof remain independent and fail-closed until their own evidence exists. Production onboarding keeps autonomy off while those proofs are incomplete.

V1 does not add or buy another VPS. The central-controller design can attach future worker nodes only after persisted evidence shows a sustained profitable queue, resource saturation, and expected incremental node profit greater than node cost. Recommendations are informational; infrastructure purchase is never automatic.

## Prohibited workloads

Central policy blocks cryptocurrency mining or earning, DePIN, bandwidth/proxy resale, unauthorized scanning/testing, spam, fake identities, fraud, prohibited local neural inference, uncontrolled browser automation, and marketplace-policy violations. The safety lane does not weaken those boundaries.

## External production proof still required

Repository code and CI cannot truthfully prove:

- public DNS, TLS, security headers, and owner-only reachability at `https://earn.amarktai.co.za`;
- an actual Webdock deploy, reboot, post-boot health, queue recovery, and external reconciliation;
- a live GenX credential, current catalog, bounded production call, model result, and actual credit reconciliation;
- current official availability, auth, automation policy, account/KYC status, South African payout eligibility, and non-crypto payout rail for each market retained for production;
- real opportunity discovery plus one explicitly permitted irreversible acquisition;
- a real funded job, production worker/GenX execution, remote submission, approval, payout, and bank/rail-confirmed `SETTLED` cash.

No demo earnings, payout readiness, daily revenue, or fake settlement evidence is seeded.

License: see [LICENSE](LICENSE).
