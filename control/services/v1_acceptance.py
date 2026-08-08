from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

import redis
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection
from django.utils import timezone

from control.models import Job, OwnerSecurityProfile, Payout
from control.ops import SECTIONS
from control.services.admission import collect_metrics, decide_admission
from control.services.workload_policy import evaluate_text
from workers.registry import all_specs


VALID_STATUSES = {"PASS", "FAIL", "BLOCKED", "EXTERNAL_PROOF_REQUIRED"}


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    title: str
    status: str
    proof_scope: str
    evidence: str
    operator_action: str = ""

    def payload(self) -> dict[str, str]:
        return asdict(self)


def _criterion(identifier: str, title: str, status: str, scope: str, evidence: str, action: str = "") -> AcceptanceCriterion:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid acceptance status: {status}")
    return AcceptanceCriterion(identifier, title, status, scope, evidence, action)


def _ci_gate(identifier: str, title: str, evidence: str, *, ci_proven: bool) -> AcceptanceCriterion:
    if ci_proven:
        return _criterion(identifier, title, "PASS", "CI", evidence)
    return _criterion(
        identifier, title, "BLOCKED", "CI", "The implementation is present, but this invocation is not attached to the completed CI proof chain.",
        "Run the complete GitHub Actions workflow and invoke v1_acceptance --ci-proven only as its final sequential gate.",
    )


def _source_text(relative: str) -> str:
    return (Path(settings.BASE_DIR) / relative).read_text(encoding="utf-8")


def _safe_defaults() -> AcceptanceCriterion:
    expected = {
        "AMARKTAI_ENV": "development",
        "AUTONOMOUS_MODE": "OFF",
        "AGENTGIGS_AUTO_APPLY_ENABLED": "0",
        "SANDBOX_CODING_ENABLED": "0",
        "AGENTGIGS_MAX_GENX_CREDITS": "0",
        "DEPENDENCY_PREPARATION_ENABLED": "0",
    }
    values = {}
    for raw in _source_text(".env.example").splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()
    mismatches = [f"{key}={values.get(key)!r}" for key, value in expected.items() if values.get(key) != value]
    if mismatches:
        return _criterion("safe_defaults", "Conservative production defaults", "FAIL", "SOURCE", "Unsafe or missing defaults: " + ", ".join(mismatches), "Restore fail-closed defaults before deployment.")
    return _criterion("safe_defaults", "Conservative production defaults", "PASS", "SOURCE", "Autonomy, auto-apply, coding, dependency preparation, and GenX spend are disabled by default.")


def _money_truth() -> AcceptanceCriterion:
    expected_lifecycle = [
        Job.State.DISCOVERED, Job.State.EXPECTED, Job.State.CLAIMED, Job.State.AWARDED,
        Job.State.EXECUTING, Job.State.SUBMITTED, Job.State.ACCEPTED, Job.State.PAYOUT_PENDING, Job.State.SETTLED,
        Job.State.FAILED,
    ]
    payout_states = set(Payout.State.values)
    if list(Job.State.values) != expected_lifecycle or payout_states != {"EARNED", "PAYOUT_PENDING", "SETTLED", "REVERSED"}:
        return _criterion("money_truth", "Canonical money-state truth", "FAIL", "SOURCE", "Canonical job or payout states have drifted.", "Restore the reviewed economic state machine.")
    return _criterion("money_truth", "Canonical money-state truth", "PASS", "SOURCE", "SETTLED remains the only received-cash terminal state; expected, accepted, and pending values remain separate.")


def _owner_state() -> AcceptanceCriterion:
    try:
        owner_ids = get_user_model().objects.filter(is_active=True, is_staff=True).values_list("id", flat=True)
        enrolled = OwnerSecurityProfile.objects.filter(user_id__in=owner_ids, totp_confirmed_at__isnull=False).exists()
    except DatabaseError:
        return _criterion("owner_login", "Owner login and MFA enrollment", "BLOCKED", "RUNTIME_CONFIG", "The owner security tables are unavailable.", "Apply migrations in the target environment, then bootstrap the owner.")
    if enrolled:
        return _criterion("owner_login", "Owner login and MFA enrollment", "PASS", "RUNTIME_CONFIG", "An active staff owner with confirmed TOTP is present.")
    return _criterion("owner_login", "Owner login and MFA enrollment", "BLOCKED", "RUNTIME_CONFIG", "No active staff owner with confirmed TOTP is present in this database.", "Run bootstrap_owner interactively in the target environment and retain recovery codes securely.")


def _postgres_redis(*, ci_proven: bool) -> AcceptanceCriterion:
    postgres_ok = False
    redis_ok = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            postgres_ok = connection.vendor == "postgresql" and cursor.fetchone() == (1,)
    except Exception:
        postgres_ok = False
    try:
        client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_connect_timeout=3, socket_timeout=3)
        redis_ok = bool(client.ping())
    except Exception:
        redis_ok = False
    if not postgres_ok or not redis_ok:
        missing = ", ".join(name for name, ok in (("PostgreSQL", postgres_ok), ("Redis", redis_ok)) if not ok)
        return _criterion("postgres_redis", "PostgreSQL and Redis state services", "BLOCKED", "RUNTIME", f"Unavailable or incorrect backend: {missing}.", "Run against the target PostgreSQL and Redis services, then repeat the acceptance command.")
    return _ci_gate("postgres_redis", "PostgreSQL and Redis state services", "PostgreSQL and Redis are reachable and the completed CI chain proved migrations, health, locking, and durable state behavior.", ci_proven=ci_proven)


def _resource_governor(*, ci_proven: bool) -> AcceptanceCriterion:
    try:
        metrics = collect_metrics()
        decision = decide_admission(purpose="ACCEPTANCE", metrics=metrics, persist=False)
    except DatabaseError:
        return _criterion("resource_governor", "Storage and resource admission", "BLOCKED", "RUNTIME", "Resource admission tables are unavailable.", "Apply migrations and repeat the governor probe.")
    except Exception as exc:
        return _criterion("resource_governor", "Storage and resource admission", "FAIL", "RUNTIME", f"Governor probe raised {exc.__class__.__name__}.", "Repair the governor probe before enabling work.")
    if not decision.allowed:
        return _criterion("resource_governor", "Storage and resource admission", "BLOCKED", "RUNTIME", "Current resource blockers: " + ", ".join(decision.reason_codes), "Free capacity or adjust reviewed VPS thresholds; do not bypass admission.")
    return _ci_gate("resource_governor", "Storage and resource admission", "Current resource probe is green and CI proved quota, memory, load, queue, and recovery decisions.", ci_proven=ci_proven)


def _worker_registry(*, ci_proven: bool) -> AcceptanceCriterion:
    expected = {"structured_data", "documents", "research", "localization", "transcription", "code_small", "code_heavy", "ci_testing", "media"}
    specs = all_specs()
    actual = {spec.worker_class for spec in specs}
    invalid = sorted(spec.worker_class for spec in specs if not spec.operations or not spec.qa_profile or not spec.factory)
    if actual != expected or invalid:
        return _criterion("worker_execution", "Registered worker execution paths", "FAIL", "SOURCE", f"Registry mismatch: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}, invalid={invalid}.", "Restore complete registered V1 worker specifications.")
    return _ci_gate("worker_execution", "Registered worker execution paths", "All nine V1 worker classes have production factories, operations, QA profiles, and completed deterministic/integration/container proofs.", ci_proven=ci_proven)


def _dashboard_contract(*, ci_proven: bool) -> AcceptanceCriterion:
    expected = {
        "overview", "live-work", "agents", "markets", "earnings", "treasury", "genx",
        "nodes", "storage", "performance", "logs", "alerts", "settings", "security",
    }
    if set(SECTIONS) != expected:
        return _criterion("dashboard", "Database-backed operations dashboard", "FAIL", "SOURCE", f"Dashboard section mismatch: missing={sorted(expected - set(SECTIONS))}, extra={sorted(set(SECTIONS) - expected)}.", "Restore every required database-backed operations section.")
    return _ci_gate("dashboard", "Database-backed operations dashboard", "CI proved all operations sections render persisted worker, market, execution, QA, resource, security, GenX, and money truth without secrets.", ci_proven=ci_proven)


def _sandbox_contract(*, ci_proven: bool) -> AcceptanceCriterion:
    broker = _source_text("sandbox_broker/server.py")
    workflow = _source_text(".github/workflows/ci.yml")
    required = ("--read-only", '"--cap-drop", "ALL"', "no-new-privileges:true", "--pids-limit", "--memory", "--cpus", "dependency-prep-smoke.py")
    missing = [marker for marker in required if marker not in broker and marker not in workflow]
    if missing or "/var/run/docker.sock:/var/run/docker.sock" in _source_text("sandbox/Dockerfile"):
        return _criterion("sandbox_isolation", "Coding sandbox isolation and secret exclusion", "FAIL", "SOURCE", "Sandbox contract markers missing or an agent image references the Docker socket: " + ", ".join(missing), "Restore the trusted-broker isolation boundary.")
    return _ci_gate("sandbox_isolation", "Coding sandbox isolation and secret exclusion", "The completed container gate built Aider/OpenHands/broker images, proved non-root bounded agents without a socket or controller secrets, and exercised dependency caches.", ci_proven=ci_proven)


def _prohibited_workloads() -> AcceptanceCriterion:
    samples = {
        "PROHIBITED_CRYPTO_MINING": "run a crypto mining pool",
        "PROHIBITED_DEPIN": "operate a depin node",
        "PROHIBITED_BANDWIDTH_RESALE": "sell unused bandwidth",
        "PROHIBITED_UNAUTHORIZED_SCANNING": "scan random networks",
        "PROHIBITED_SPAM": "launch a spam campaign",
        "PROHIBITED_FAKE_IDENTITY": "create a fake identity",
        "PROHIBITED_FRAUD": "enable fraud with stolen cards",
        "PROHIBITED_LOCAL_INFERENCE": "run an llm locally",
        "PROHIBITED_BROWSER_AUTOMATION": "uncontrolled browser automation",
    }
    failures = [code for code, sample in samples.items() if code not in evaluate_text(sample).reason_codes]
    if failures or not evaluate_text("resize an owned product image to 1200x628").allowed:
        return _criterion("prohibited_workloads", "Prohibited workload enforcement", "FAIL", "SOURCE", "Policy regression for: " + ", ".join(failures or ["benign workload"]), "Restore centralized fail-closed workload policy coverage.")
    return _criterion("prohibited_workloads", "Prohibited workload enforcement", "PASS", "SOURCE", "All nine prohibited infrastructure/work categories are blocked while a benign bounded media task remains allowed.")


def _readme_truth() -> AcceptanceCriterion:
    readme = _source_text("README.md")
    markers = ("CODE-COMPLETE", "CI-PROVEN", "disabled by default", "EXTERNAL_PROOF_REQUIRED", "Only `SETTLED`")
    missing = [marker for marker in markers if marker not in readme]
    if missing:
        return _criterion("readme_truth", "README implementation truth", "FAIL", "SOURCE", "README is missing truth markers: " + ", ".join(missing), "Update README without claiming external production or payout proof.")
    return _criterion("readme_truth", "README implementation truth", "PASS", "SOURCE", "README distinguishes implementation, CI proof, safe defaults, and external proof without claiming live revenue.")


def build_acceptance_report(*, ci_proven: bool = False) -> dict:
    criteria = [
        _safe_defaults(),
        _money_truth(),
        _owner_state(),
        _ci_gate("jwt_replay", "JWT refresh rotation and replay-family revocation", "CI proved access/refresh rotation, replay detection, and family revocation.", ci_proven=ci_proven),
        _ci_gate("totp_recovery", "TOTP and one-time recovery", "CI proved password pre-authentication, TOTP, invalid-code rejection, and one-time recovery-code consumption.", ci_proven=ci_proven),
        _postgres_redis(ci_proven=ci_proven),
        _resource_governor(ci_proven=ci_proven),
        _ci_gate("acquisition_gates", "Autonomy, capability preflight, economics, and global locks", "CI proved OFF/SHADOW/LOW_RISK/FULL semantics, separate market opt-in, exact registry preflight, profitability gates, and fencing locks.", ci_proven=ci_proven),
        _worker_registry(ci_proven=ci_proven),
        _ci_gate("qa_repair", "Independent QA and bounded repair", "CI proved independent QA records, bounded repair attempts, and fail-closed submission gating.", ci_proven=ci_proven),
        _ci_gate("lifecycle_logic", "Submission, revision, payout, and ledger lifecycle logic", "CI proved idempotent lifecycle transitions and that only reconciled SETTLED payouts become received cash.", ci_proven=ci_proven),
        _dashboard_contract(ci_proven=ci_proven),
        _ci_gate("watchdog_recovery", "Watchdog, reconciliation, and retention", "CI proved stale-state recovery, safe retry classification, unknown-remote reconciliation, cleanup exclusions, and audit records.", ci_proven=ci_proven),
        _sandbox_contract(ci_proven=ci_proven),
        _ci_gate("media_runtime", "Bounded deterministic media runtime", "CI built the FFmpeg/Pillow production image and proved image/audio output plus independent QA and output bounds.", ci_proven=ci_proven),
        _prohibited_workloads(),
        _readme_truth(),
        _criterion("public_https", "Public HTTPS at earn.amarktai.co.za", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "Repository and Caddy configuration cannot prove public DNS/TLS reachability.", "Deploy to Webdock, verify DNS, TLS certificate, security headers, and owner-only access from outside the VPS."),
        _criterion("actual_reboot", "Actual Webdock reboot recovery", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "CI proves restart configuration and reconciliation logic, not a physical VPS reboot.", "Perform a controlled Webdock reboot and retain service, heartbeat, queue, and reconciliation evidence."),
        _criterion("live_genx", "Live GenX credentials and budgeted call", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "No live credential or billable call is exercised by CI.", "Configure the production GenX key, sync the live catalog, execute one capped call, and reconcile actual credits."),
        _criterion("live_market_account", "Live AgentGigs authentication, policy, KYC, and payout readiness", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "Marketplace onboarding and South African payout eligibility require the real account.", "Complete account verification/KYC, confirm current automation policy, and persist payout-ready evidence without exposing credentials."),
        _criterion("live_opportunity", "Real opportunity discovery and acquisition", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "CI uses deterministic fixtures and never performs a live irreversible marketplace mutation.", "Begin in SHADOW, validate real discovered opportunities, then explicitly enable LOW_RISK and the separate market switch for a funded eligible job."),
        _criterion("settled_cash", "Real execution, remote submission, approval, payout, and SETTLED cash", "EXTERNAL_PROOF_REQUIRED", "PRODUCTION", "Code and CI cannot manufacture a funded job or received cash.", "Complete one real funded job through remote submission, approval, payout reconciliation, and bank/rail-confirmed SETTLED posting."),
    ]
    counts = {status: sum(1 for row in criteria if row.status == status) for status in sorted(VALID_STATUSES)}
    overall = "FAIL" if counts["FAIL"] else "BLOCKED" if counts["BLOCKED"] else "EXTERNAL_PROOF_REQUIRED" if counts["EXTERNAL_PROOF_REQUIRED"] else "PASS"
    return {
        "schema_version": 1,
        "generated_at": timezone.now().isoformat(),
        "ci_proven_context": ci_proven,
        "overall_status": overall,
        "counts": counts,
        "criteria": [row.payload() for row in criteria],
        "terminology": {
            "CODE-COMPLETE": "Implementation exists in the repository.",
            "CI-PROVEN": "The required deterministic/stateful/container gates completed in one sequential CI run.",
            "SOURCE-WIRED": "A real external adapter path exists but may lack live proof.",
            "RUNTIME-PROVEN": "The target production environment exercised the path successfully.",
            "PAYOUT-PROVEN": "A real marketplace payout was reconciled.",
            "SETTLED": "Cash was actually received and reconciled.",
        },
    }
