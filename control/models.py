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
    expected_profit = models.DecimalField(max_digits=14, decimal_places=2)
    expected_minutes = models.PositiveIntegerField()
    score_version = models.CharField(max_length=32, default="v1")

class JobLock(Timestamped):
    job = models.OneToOneField(Job, on_delete=models.CASCADE)
    node_id = models.CharField(max_length=120)
    lease_until = models.DateTimeField()
    fencing_token = models.PositiveBigIntegerField(default=1)

class Worker(Timestamped):
    id = models.CharField(max_length=120, primary_key=True)
    worker_class = models.CharField(max_length=80)
    version = models.CharField(max_length=40, default="0.1.0")
    node = models.CharField(max_length=120, default="VPS1")
    status = models.CharField(max_length=32, default="OFFLINE")
    current_job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.SET_NULL)
    last_heartbeat = models.DateTimeField(default=timezone.now)

class GenXCall(Timestamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.SET_NULL)
    worker = models.ForeignKey(Worker, null=True, blank=True, on_delete=models.SET_NULL)
    model = models.CharField(max_length=120)
    external_job_id = models.CharField(max_length=255, blank=True)
    credits = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    usage = models.JSONField(default=dict)
    latency_ms = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=32)
    cost_equivalent = models.DecimalField(max_digits=14, decimal_places=4, default=0)

class QAResult(Timestamped):
    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    check_type = models.CharField(max_length=100)
    passed = models.BooleanField()
    score = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    evidence = models.JSONField(default=dict)

class Submission(Timestamped):
    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    remote_id = models.CharField(max_length=255, blank=True)
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=32, default="PENDING")

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
    state = models.CharField(max_length=32, choices=State.choices)
    expected_date = models.DateField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)

class LedgerEntry(Timestamped):
    reference = models.CharField(max_length=160, db_index=True)
    account = models.CharField(max_length=120)
    counter_account = models.CharField(max_length=120)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    event_type = models.CharField(max_length=80)
    metadata = models.JSONField(default=dict)

class AuditEvent(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    severity = models.CharField(max_length=16, default="INFO")
    event_type = models.CharField(max_length=120)
    actor = models.CharField(max_length=120, blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4)
    metadata = models.JSONField(default=dict)
