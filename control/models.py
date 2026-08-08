import uuid
from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone

User = get_user_model()


class Timestamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class OwnerSecurityProfile(Timestamped):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    totp_secret_encrypted = models.TextField(blank=True)
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)
    security_version = models.PositiveIntegerField(default=1)


class RecoveryCode(Timestamped):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    code_hash = models.CharField(max_length=255)
    used_at = models.DateTimeField(null=True, blank=True)


class LoginChallenge(Timestamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)


class RefreshSession(Timestamped):
    jti = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    family_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    replaced_by_jti = models.UUIDField(null=True, blank=True)
    user_agent_hash = models.CharField(max_length=64, blank=True)
    ip_hash = models.CharField(max_length=64, blank=True)


class Marketplace(Timestamped):
    class Status(models.TextChoices):
        LIVE = "LIVE"
        WATCH_ONLY = "WATCH_ONLY"
        PAYOUT_BLOCKED = "PAYOUT_BLOCKED"
        AUTH_EXPIRED = "AUTH_EXPIRED"
        DEGRADED = "DEGRADED"
        NO_SUPPLY = "NO_SUPPLY"
        POLICY_DISABLED = "POLICY_DISABLED"
        UNPROFITABLE = "UNPROFITABLE"

    slug = models.SlugField(unique=True)
    display_name = models.CharField(max_length=100)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.WATCH_ONLY)
    enabled = models.BooleanField(default=False)
    payout_ready = models.BooleanField(default=False)
    south_africa_verified = models.BooleanField(default=False)
    fee_rate = models.DecimalField(max_digits=8, decimal_places=6, default=0)
    payment_model = models.CharField(max_length=100, blank=True)
    policy_hash = models.CharField(max_length=128, blank=True)
    last_policy_check = models.DateTimeField(null=True, blank=True)


class MarketplaceCredential(Timestamped):
    marketplace = models.ForeignKey(Marketplace, on_delete=models.CASCADE, related_name="credentials")
    credential_type = models.CharField(max_length=80)
    encrypted_value = models.TextField()
    key_id = models.CharField(max_length=64, blank=True)
    active = models.BooleanField(default=True)
    rotated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["marketplace", "credential_type"],
                condition=models.Q(active=True),
                name="uniq_active_market_credential_type",
            )
        ]


class MarketPolicyVersion(Timestamped):
    marketplace = models.ForeignKey(Marketplace, on_delete=models.CASCADE, related_name="policy_versions")
    policy_hash = models.CharField(max_length=128)
    source_url = models.URLField(blank=True)
    automation_allowed = models.BooleanField(default=False)
    webdock_compatible = models.BooleanField(default=False)
    checked_at = models.DateTimeField(default=timezone.now)
    snapshot = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["marketplace", "policy_hash"], name="uniq_market_policy_hash")
        ]


class MarketHealth(Timestamped):
    marketplace = models.OneToOneField(Marketplace, on_delete=models.CASCADE, related_name="health_snapshot")
    api_ok = models.BooleanField(default=False)
    auth_ok = models.BooleanField(default=False)
    payout_ok = models.BooleanField(default=False)
    supply_ok = models.BooleanField(default=False)
    latency_ms = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=120, blank=True)
    checked_at = models.DateTimeField(default=timezone.now)
    details = models.JSONField(default=dict)


class PayoutAccount(Timestamped):
    marketplace = models.ForeignKey(Marketplace, on_delete=models.CASCADE, related_name="payout_accounts")
    rail = models.CharField(max_length=80)
    external_reference = models.CharField(max_length=255, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=32, default="PENDING")
    south_africa_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    details = models.JSONField(default=dict)


class MarketCandidate(Timestamped):
    name = models.CharField(max_length=120)
    url = models.URLField()
    status = models.CharField(max_length=32, default="MARKET_CANDIDATE")
    api_or_mcp = models.BooleanField(default=False)
    payout_method = models.CharField(max_length=120, blank=True)
    south_africa_support = models.BooleanField(default=False)
    automation_allowed = models.BooleanField(default=False)
    webdock_compatible = models.BooleanField(default=False)
    notes = models.TextField(blank=True)


class Job(Timestamped):
    class State(models.TextChoices):
        DISCOVERED = "DISCOVERED"
        EXPECTED = "EXPECTED"
        CLAIMED = "CLAIMED"
        AWARDED = "AWARDED"
        EXECUTING = "EXECUTING"
        SUBMITTED = "SUBMITTED"
        ACCEPTED = "ACCEPTED"
        PAYOUT_PENDING = "PAYOUT_PENDING"
        SETTLED = "SETTLED"
        FAILED = "FAILED"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    marketplace = models.ForeignKey(Marketplace, on_delete=models.PROTECT)
    external_id = models.CharField(max_length=255)
    title = models.CharField(max_length=500)
    task_class = models.CharField(max_length=100)
    reward = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    deadline = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=32, choices=State.choices, default=State.DISCOVERED)
    normalized_payload = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["marketplace", "external_id"], name="uniq_market_external_job")]


class JobScore(Timestamped):
    job = models.OneToOneField(Job, on_delete=models.CASCADE)
    p_acquire = models.DecimalField(max_digits=6, decimal_places=5)
    p_accept = models.DecimalField(max_digits=6, decimal_places=5)
    p_payment = models.DecimalField(max_digits=6, decimal_places=5)
    expected_genx_cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    expected_external_cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    expected_cash = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    expected_profit = models.DecimalField(max_digits=14, decimal_places=2)
    expected_profit_per_minute = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    expected_profit_per_genx_credit = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    expected_minutes = models.PositiveIntegerField()
    max_genx_credits = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    decision = models.CharField(max_length=32, default="WATCH")
    reason_codes = models.JSONField(default=list)
    score_version = models.CharField(max_length=32, default="v1")


class JobLock(Timestamped):
    job = models.OneToOneField(Job, on_delete=models.CASCADE)
    node_id = models.CharField(max_length=120)
    lease_until = models.DateTimeField()
    fencing_token = models.PositiveBigIntegerField(default=1)


class Application(Timestamped):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="applications")
    action = models.CharField(max_length=32, default="APPLY")
    offered_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    message = models.TextField(blank=True)
    remote_reference = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, default="PENDING")


class Bid(Timestamped):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="bids")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    remote_reference = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, default="PENDING")


class Claim(Timestamped):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="claims")
    remote_reference = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, default="CLAIMED")
    claimed_at = models.DateTimeField(default=timezone.now)


class JobMessage(Timestamped):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="messages")
    remote_id = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=80)
    direction = models.CharField(max_length=16, choices=[("IN", "Inbound"), ("OUT", "Outbound")])
    content_hash = models.CharField(max_length=64)
    content = models.TextField(blank=True)
    action_required = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "remote_id"],
                condition=~models.Q(remote_id=""),
                name="uniq_job_remote_message",
            )
        ]


class Node(Timestamped):
    id = models.CharField(max_length=120, primary_key=True)
    hostname = models.CharField(max_length=255)
    release_version = models.CharField(max_length=80, blank=True)
    role_profile = models.CharField(max_length=80, default="controller-worker")
    health = models.CharField(max_length=32, default="UNKNOWN")
    cpu_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    ram_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    disk_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    last_heartbeat = models.DateTimeField(default=timezone.now)


class Worker(Timestamped):
    id = models.CharField(max_length=120, primary_key=True)
    worker_class = models.CharField(max_length=80)
    version = models.CharField(max_length=40, default="0.1.0")
    node = models.CharField(max_length=120, default="VPS1")
    status = models.CharField(max_length=32, default="OFFLINE")
    current_job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.SET_NULL)
    last_heartbeat = models.DateTimeField(default=timezone.now)


class WorkerVersion(Timestamped):
    worker_class = models.CharField(max_length=80)
    version = models.CharField(max_length=40)
    git_sha = models.CharField(max_length=64, blank=True)
    active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["worker_class", "version"], name="uniq_worker_class_version")]


class Execution(Timestamped):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="executions")
    worker = models.ForeignKey(Worker, null=True, blank=True, on_delete=models.SET_NULL)
    node_id = models.CharField(max_length=120, default="VPS1")
    attempt = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=32, default="QUEUED")
    workspace = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    result = models.JSONField(default=dict)
    error_code = models.CharField(max_length=120, blank=True)
    error_detail = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "attempt"], name="uniq_job_execution_attempt")]


class Artifact(Timestamped):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="artifacts")
    execution = models.ForeignKey(Execution, null=True, blank=True, on_delete=models.SET_NULL, related_name="artifacts")
    kind = models.CharField(max_length=80, default="deliverable")
    path = models.CharField(max_length=700, blank=True)
    url = models.URLField(blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True)
    accepted = models.BooleanField(default=False)
    retention_class = models.CharField(max_length=32, default="JOB_EVIDENCE")


class GenXModelCatalog(Timestamped):
    model_id = models.CharField(max_length=160, primary_key=True)
    category = models.CharField(max_length=40, blank=True, db_index=True)
    provider = models.CharField(max_length=120, blank=True)
    active = models.BooleanField(default=True)
    price_hint = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    model_payload = models.JSONField(default=dict)
    pricing_payload = models.JSONField(default=dict)
    last_seen_at = models.DateTimeField(default=timezone.now)


class GenXAccountSnapshot(Timestamped):
    available_credits = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    raw = models.JSONField(default=dict)


class GenXCall(Timestamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_key = models.CharField(max_length=180, null=True, blank=True, unique=True)
    job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.SET_NULL)
    worker = models.ForeignKey(Worker, null=True, blank=True, on_delete=models.SET_NULL)
    model = models.CharField(max_length=160)
    task_class = models.CharField(max_length=100, blank=True)
    marketplace_slug = models.CharField(max_length=100, blank=True)
    external_job_id = models.CharField(max_length=255, blank=True)
    estimated_credits = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    max_allowed_credits = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    credits = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    usage = models.JSONField(default=dict)
    requested_metadata = models.JSONField(default=dict)
    result_url = models.URLField(blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=32)
    cost_equivalent = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=120, blank=True)


class ModelStat(Timestamped):
    model = models.CharField(max_length=160)
    task_class = models.CharField(max_length=100)
    attempts = models.PositiveBigIntegerField(default=0)
    accepted = models.PositiveBigIntegerField(default=0)
    revenue = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    credits = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    cost_equivalent = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    profit = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    total_latency_ms = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["model", "task_class"], name="uniq_model_task_stat")]


class QAResult(Timestamped):
    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    execution = models.ForeignKey(Execution, null=True, blank=True, on_delete=models.SET_NULL)
    check_type = models.CharField(max_length=100)
    passed = models.BooleanField()
    score = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    evidence = models.JSONField(default=dict)


class Submission(Timestamped):
    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    artifact = models.ForeignKey(Artifact, null=True, blank=True, on_delete=models.SET_NULL)
    remote_id = models.CharField(max_length=255, blank=True)
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=32, default="PENDING")
    response = models.JSONField(default=dict)


class Revision(Timestamped):
    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    message = models.TextField()
    status = models.CharField(max_length=32, default="REQUIRED")


class Payout(Timestamped):
    class State(models.TextChoices):
        EARNED = "EARNED"
        PAYOUT_PENDING = "PAYOUT_PENDING"
        SETTLED = "SETTLED"
        REVERSED = "REVERSED"

    job = models.ForeignKey(Job, on_delete=models.PROTECT)
    gross = models.DecimalField(max_digits=14, decimal_places=2)
    fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    external_reference = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=32, choices=State.choices)
    expected_date = models.DateField(null=True, blank=True)
    earned_at = models.DateTimeField(null=True, blank=True)
    pending_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "currency"], name="uniq_job_payout_currency")]


class LedgerEntry(Timestamped):
    entry_key = models.CharField(max_length=200, unique=True, null=True, blank=True)
    reference = models.CharField(max_length=160, db_index=True)
    account = models.CharField(max_length=120)
    counter_account = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    event_type = models.CharField(max_length=80)
    metadata = models.JSONField(default=dict)


class TreasuryBalance(Timestamped):
    marketplace = models.ForeignKey(Marketplace, null=True, blank=True, on_delete=models.SET_NULL)
    account = models.CharField(max_length=120)
    currency = models.CharField(max_length=3, default="USD")
    earned = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    pending = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    settled = models.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["account", "currency"], name="uniq_treasury_account_currency")]


class Alert(Timestamped):
    severity = models.CharField(max_length=16, default="INFO")
    alert_type = models.CharField(max_length=120)
    status = models.CharField(max_length=32, default="OPEN")
    message = models.TextField()
    metadata = models.JSONField(default=dict)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)


class SystemSetting(Timestamped):
    key = models.CharField(max_length=160, unique=True)
    value = models.JSONField(default=dict)
    sensitive = models.BooleanField(default=False)


class AuditEvent(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    severity = models.CharField(max_length=16, default="INFO")
    event_type = models.CharField(max_length=120)
    actor = models.CharField(max_length=120, blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4)
    metadata = models.JSONField(default=dict)
