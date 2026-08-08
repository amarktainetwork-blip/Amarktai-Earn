# Amarktai Earn

Amarktai Earn is a private, owner-only controller for legitimate paid digital work. Its objective is **net settled profit**, with PostgreSQL as the source of truth and a fail-closed path from discovery through payment reconciliation.

Core V1 is **CODE-COMPLETE** in this repository. The required GitHub Actions workflow is the merge gate for **CI-PROVEN** behavior. This does not mean the system is deployed, marketplace-approved, payout-ready, or earning money. Those facts require production evidence and remain `EXTERNAL_PROOF_REQUIRED` until exercised.

## Truth vocabulary

- **CODE-COMPLETE** — the implementation exists.
- **CI-PROVEN** — deterministic, PostgreSQL/Redis, and container gates passed in one sequential GitHub Actions run.
- **SOURCE-WIRED** — a real external adapter path exists.
- **RUNTIME-PROVEN** — the target production environment exercised the path successfully.
- **PAYOUT-PROVEN** — a real marketplace payout was reconciled.
- **SETTLED** — cash was actually received and reconciled.

Only `SETTLED` is received cash. Discovered value, expected profit, claims, awards, accepted work, and payout-pending balances are never presented as settled revenue.

## Core flow

```text
discover opportunity
  -> classify exact capability
  -> enforce workload, market, payout, autonomy, economics, budget, and resource gates
  -> acquire only when policy permits
  -> stage and hash source material
  -> create deterministic WorkPlan
  -> execute registered worker in its approved runtime
  -> persist Artifact
  -> run independent QA
  -> bounded repair when eligible
  -> submit only after QA
  -> handle revisions as revenue protection
  -> reconcile acceptance, payout pending, and actual settlement separately
```

Markets are adapters. Workers are replaceable registered capabilities. Interfaces do not select providers or models directly; the controller routes by capability, policy, quality, economics, resources, history, and risk.

## Registered V1 workers

| Worker | Production implementation | Independent QA | Runtime boundary |
|---|---|---|---|
| Structured data | JSON/CSV conversion and normalization | deterministic CSV | controller worker |
| Documents | PDF/DOCX/TXT/Markdown extraction, summary, rewrite | document QA | bounded GenX where required |
| Research | web-assisted cited reports | citation/source QA | budgeted GenX session |
| Localisation | explicit target-language translation | structural translation QA | budgeted GenX |
| Transcription | bounded audio/video transcription | transcript QA | budgeted GenX upload lifecycle |
| Small code | Aider repository changes | independent patch/test QA | constrained sandbox |
| Heavy code | OpenHands repository changes | independent patch/test QA | constrained sandbox |
| CI/testing | repository test execution | test-result QA | network-isolated sandbox |
| Media | bounded image resize/crop/convert/compress and FFmpeg trim/transcode/audio extraction | decoded media/format/dimension/duration/stream QA | CPU/time/file-size bounded |

Coding agents never receive the Docker socket, controller filesystem, or controller master secrets. A trusted deterministic broker alone controls disposable containers. Supported Python and npm dependencies are prepared from verified snapshots and recognized lock manifests in isolated fetch containers; resulting caches are mounted read-only. Project install hooks, unpinned Python requirements, npm lifecycle scripts, unsupported ecosystems, and unsafe manifests fail closed.

## Operations and security

The controller includes:

- centralized resource admission for acquisition, WorkPlan queuing, GenX, sandboxes, and media;
- disk, storage-class quota, memory, CPU/load, queue, sandbox, GenX, media, and per-market concurrency limits;
- persisted blocker reasons, alerts, resource snapshots, and audit events;
- watchdog recovery for stale heartbeats, leases, plans, executions, queue ownership, sandbox containers, temporary files, and defined unknown-remote reconciliation;
- retention rules that preserve ledgers, payouts, audits, mutation records, disputed source material, accepted artifacts, and settlement evidence;
- Argon2id owner passwords, TOTP, one-time recovery codes, short-lived JWT access, rotating refresh sessions, replay-family revocation, persistent throttles, temporary cooldowns, and password-plus-TOTP reauthentication grants;
- canonical `OFF`, `SHADOW`, `LOW_RISK`, and `FULL` autonomy policy. `FULL` never bypasses legality, marketplace policy, payout, capability, profitability, budget, resource, credential, or sandbox gates;
- database-backed dashboard sections for overview, live work, agents, markets, earnings, treasury, GenX, nodes, storage, performance, logs, alerts, settings, and security.

Sensitive values are never returned by dashboard snapshots. Their state is shown only as `CONFIGURED — HIDDEN` or `NOT CONFIGURED`.

## Safe defaults

The repository is disabled by default for risky or billable behavior:

```dotenv
AMARKTAI_ENV=development
AUTONOMOUS_MODE=OFF
AGENTGIGS_AUTO_APPLY_ENABLED=0
SANDBOX_CODING_ENABLED=0
DEPENDENCY_PREPARATION_ENABLED=0
AGENTGIGS_MAX_GENX_CREDITS=0
```

A fresh clone cannot spend GenX credits, acquire work, submit marketplace mutations, or launch coding jobs without explicit operator configuration. AgentGigs auto-apply remains a separate opt-in even under `LOW_RISK` or `FULL`.

## Acceptance command

Run the honest environment report with:

```bash
python manage.py v1_acceptance --format text
python manage.py v1_acceptance --format json
```

Statuses are `PASS`, `FAIL`, `BLOCKED`, and `EXTERNAL_PROOF_REQUIRED`. The command exits non-zero on `FAIL` by default; use `--fail-on BLOCKED` for a configured production readiness gate.

`--ci-proven` is reserved for the final sequential GitHub Actions step, after all tests, image/isolation smokes, secret exclusion, and encrypted backup/restore have passed. It must not be used to claim production runtime proof.

The CI workflow proves compilation, Django checks, migration drift/migrations, PostgreSQL and Redis health, authentication/replay/security transitions, market lifecycle logic, asset staging, WorkPlan/execution/QA/repair, dashboard truth, workers, resource governor, watchdog/recovery, autonomy/acquisition policy, prohibited workloads, dependency preparation, media, production preflight, Compose validity, sandbox isolation, production image contents, `.env` exclusion, and encrypted backup/clean restore validation.

## External production proof still required

Repository code and CI cannot truthfully prove:

- public DNS and HTTPS at `https://earn.amarktai.co.za`;
- an actual Webdock reboot and post-boot reconciliation;
- a live production GenX credential, catalog sync, capped call, and credit reconciliation;
- live AgentGigs authentication, current automation policy, KYC, South African eligibility, and payout readiness;
- real opportunity discovery and a permitted irreversible acquisition;
- a real funded job, remote submission, approval, payout, and reconciled `SETTLED` cash.

The acceptance command prints the exact operator action for each remaining external item. No demo earnings or fake payout evidence are seeded.

## Deployment and verification

Copy `.env.example` to `.env`, replace every production secret, keep autonomy off while onboarding, and run:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose exec web python manage.py production_check
docker compose exec web python manage.py v1_acceptance --format text
```

Compose uses persistent PostgreSQL/Redis/application volumes, non-root long-running services, health-aware ordering, migrations/preflight, and restart policies. `scripts/backup.sh` and `scripts/restore.sh` provide encrypted PostgreSQL backup/restore; an offsite retention policy and actual restore drill remain operator responsibilities.

## Prohibited autonomous workloads

The centralized workload policy blocks cryptocurrency mining, DePIN, bandwidth/proxy resale, unauthorized scanning, spam, fake identities, fraud, prohibited local neural inference, uncontrolled browser automation, and work that violates the applicable marketplace automation policy. These boundaries are deterministic regression gates, not a broad content-moderation system.

## Known future expansion

Future work is evidence-driven rather than required for core V1: additional payout-ready market adapters, optional passkeys, generative media only when a suitable live GenX capability is proven, offsite backup automation, and extra worker nodes only after measured capacity demand.

License: see [LICENSE](LICENSE).
