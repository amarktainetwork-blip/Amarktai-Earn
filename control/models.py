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


class AuthThrottle(Timestamped):
    key_hash = models.CharField(max_length=64, unique=True)
    scope = models.CharField(max_length=40)
    failure_count = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField(default=timezone.now)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)


class ReauthenticationGrant(Timestamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=64, unique=True)
    allowed_actions = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)


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
    fingerprint = models.CharField(max_length=32, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    last_test_at = models.DateTimeField(null=True, blank=True)

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


class MarketIntegrationProfile(Timestamped):
    """Versioned truth about an adapter surface and its external launch blockers."""

    marketplace = models.OneToOneField(Marketplace, on_delete=models.CASCADE, related_name="integration_profile")
    adapter_name = models.CharField(max_length=160)
    adapter_version = models.CharField(max_length=40, default="v1")
    source_wired = models.BooleanField(default=False)
    autonomous_acquisition_enabled = models.BooleanField(default=False)
    policy_verified = models.BooleanField(default=False)
    docs_checked_at = models.DateTimeField(default=timezone.now)
    auth_method = models.CharField(max_length=160, blank=True)
    rate_limit = models.CharField(max_length=160, blank=True)
    payout_method = models.CharField(max_length=160, blank=True)
    capabilities = models.JSONField(default=dict)
    source_urls = models.JSONField(default=list)
    blockers = models.JSONField(default=list)
    evidence = models.JSONField(default=dict)
    revenue_channels = models.JSONField(default=list)
    seller_capabilities = models.JSONField(default=dict)
    automation_status = models.CharField(max_length=80, default="BLOCKED")
    job_acquisition_mode = models.CharField(max_length=80, blank=True)
    seller_mode = models.CharField(max_length=80, blank=True)
    settlement_rail = models.CharField(max_length=120, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    hosting_policy = models.CharField(max_length=40, default="UNVERIFIED")
    api_contract_state = models.CharField(max_length=80, default="UNVERIFIED")
    payout_proof_state = models.CharField(max_length=80, default="UNVERIFIED")
    manual_onboarding_required = models.BooleanField(default=True)
    category = models.CharField(max_length=80, default="EARNING_CHANNEL")
    classification = models.CharField(max_length=40, default="INACTIVE")
    setup_state = models.CharField(max_length=80, default="NOT_STARTED")
    credential_state = models.CharField(max_length=40, default="NOT_CONFIGURED")
    kyc_state = models.CharField(max_length=40, default="UNKNOWN")
    api_connection_state = models.CharField(max_length=40, default="UNVERIFIED")
    webhook_state = models.CharField(max_length=40, default="NOT_APPLICABLE")
    payout_configuration_state = models.CharField(max_length=40, default="UNVERIFIED")
    payout_receipt_proof_state = models.CharField(max_length=40, default="UNVERIFIED")
    work_capability_state = models.CharField(max_length=40, default="UNVERIFIED")
    live_proving_state = models.CharField(max_length=40, default="BLOCKED")
    last_connection_status = models.CharField(max_length=40, blank=True)
    last_connection_test_at = models.DateTimeField(null=True, blank=True)
    last_connection_success_at = models.DateTimeField(null=True, blank=True)
    last_error_category = models.CharField(max_length=80, blank=True)
    last_safe_error = models.CharField(max_length=300, blank=True)
    last_reconciled_at = models.DateTimeField(null=True, blank=True)
    owner_action_required = models.CharField(max_length=500, blank=True)
    off_host_requirements = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(autonomous_acquisition_enabled=False)
                | models.Q(policy_verified=True),
                name="market_acquisition_requires_verified_policy",
            )
        ]


class ServiceOffering(Timestamped):
    class PricingModel(models.TextChoices):
        FIXED_PROJECT = "FIXED_PROJECT"
        PER_CALL = "PER_CALL"
        PER_UNIT = "PER_UNIT"
        SUBSCRIPTION = "SUBSCRIPTION"
        OUTCOME = "OUTCOME"

    class ProofState(models.TextChoices):
        UNPROVEN = "UNPROVEN"
        SOURCE_PROVEN = "SOURCE_PROVEN"
        EXECUTION_PROVEN = "EXECUTION_PROVEN"
        SELLABLE = "SELLABLE"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True)
    display_name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    capability = models.CharField(max_length=100)
    operation = models.CharField(max_length=100)
    worker_class = models.CharField(max_length=100)
    pricing_model = models.CharField(max_length=24, choices=PricingModel.choices)
    currency = models.CharField(max_length=3, default="USD")
    advertised_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    minimum_profitable_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    platform_fee_rate = models.DecimalField(max_digits=8, decimal_places=6, default=0)
    expected_genx_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    max_genx_credits = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    expected_external_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    expected_operational_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    expected_minutes = models.PositiveIntegerField(default=1)
    sla_minutes = models.PositiveIntegerField(default=1440)
    input_schema = models.JSONField(default=dict)
    output_schema = models.JSONField(default=dict)
    terms_metadata = models.JSONField(default=dict)
    proof_evidence = models.JSONField(default=dict)
    enabled = models.BooleanField(default=False)
    accepting_orders = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    proof_state = models.CharField(max_length=24, choices=ProofState.choices, default=ProofState.UNPROVEN)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(advertised_price__gte=0), name="service_advertised_price_nonnegative"),
            models.CheckConstraint(condition=models.Q(minimum_profitable_price__gte=0), name="service_minimum_price_nonnegative"),
            models.CheckConstraint(condition=models.Q(platform_fee_rate__gte=0) & models.Q(platform_fee_rate__lt=1), name="service_fee_rate_valid"),
            models.CheckConstraint(condition=models.Q(expected_genx_cost__gte=0), name="service_genx_cost_nonnegative"),
            models.CheckConstraint(condition=models.Q(max_genx_credits__gte=0), name="service_genx_credits_nonnegative"),
            models.CheckConstraint(condition=models.Q(expected_external_cost__gte=0), name="service_external_cost_nonnegative"),
            models.CheckConstraint(condition=models.Q(expected_operational_cost__gte=0), name="service_operational_cost_nonnegative"),
        ]


class MarketServiceListing(Timestamped):
    class Status(models.TextChoices):
        DRAFT = "DRAFT"
        READY = "READY"
        PUBLISHED = "PUBLISHED"
        PAUSED = "PAUSED"
        BLOCKED = "BLOCKED"
        STALE = "STALE"
        FAILED = "FAILED"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    offering = models.ForeignKey(ServiceOffering, on_delete=models.PROTECT, related_name="market_listings")
    marketplace = models.ForeignKey(Marketplace, on_delete=models.PROTECT, related_name="service_listings")
    remote_listing_id = models.CharField(max_length=255, blank=True)
    remote_reference = models.CharField(max_length=700, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    published_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="USD")
    pricing_model = models.CharField(max_length=24, choices=ServiceOffering.PricingModel.choices)
    published_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    platform_metadata = models.JSONField(default=dict)
    remote_version = models.CharField(max_length=80, blank=True)
    policy_hash = models.CharField(max_length=128, blank=True)
    failure_code = models.CharField(max_length=120, blank=True)
    failure_detail = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["offering", "marketplace"], name="uniq_service_market_listing"),
            models.CheckConstraint(condition=models.Q(published_price__gte=0), name="service_listing_price_nonnegative"),
        ]


class InboundOrder(Timestamped):
    class Status(models.TextChoices):
        RECEIVED = "RECEIVED"
        PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
        READY = "READY"
        ACCEPTED = "ACCEPTED"
        DELIVERED = "DELIVERED"
        PAYOUT_PENDING = "PAYOUT_PENDING"
        SETTLED = "SETTLED"
        REVERSED = "REVERSED"
        FAILED = "FAILED"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    marketplace = models.ForeignKey(Marketplace, on_delete=models.PROTECT, related_name="inbound_orders")
    listing = models.ForeignKey(MarketServiceListing, null=True, blank=True, on_delete=models.PROTECT, related_name="inbound_orders")
    job = models.OneToOneField("Job", null=True, blank=True, on_delete=models.PROTECT, related_name="inbound_order")
    remote_order_id = models.CharField(max_length=255)
    idempotency_key = models.CharField(max_length=255)
    buyer_reference = models.CharField(max_length=255, blank=True)
    requirements = models.JSONField(default=dict)
    input_assets = models.JSONField(default=list)
    quoted_price = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    platform_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    funding_state = models.CharField(max_length=80, default="UNVERIFIED")
    deadline = models.DateTimeField(null=True, blank=True)
    remote_state = models.CharField(max_length=80, blank=True)
    messages = models.JSONField(default=list)
    usage = models.JSONField(default=dict)
    settlement_reference = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.RECEIVED)
    economic_preflight = models.JSONField(default=dict)
    request_digest = models.CharField(max_length=64)
    authenticated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["marketplace", "remote_order_id"], name="uniq_market_inbound_order"),
            models.UniqueConstraint(fields=["marketplace", "idempotency_key"], name="uniq_market_inbound_idempotency"),
            models.CheckConstraint(condition=models.Q(quoted_price__gt=0), name="inbound_order_price_positive"),
            models.CheckConstraint(condition=models.Q(platform_fee__gte=0) & models.Q(platform_fee__lte=models.F("quoted_price")), name="inbound_order_fee_valid"),
        ]


class InboundSettlementEvent(Timestamped):
    class State(models.TextChoices):
        AUTHORIZED = "AUTHORIZED"
        ESCROW = "ESCROW"
        PAYOUT_PENDING = "PAYOUT_PENDING"
        SETTLED = "SETTLED"
        REVERSED = "REVERSED"

    order = models.ForeignKey(InboundOrder, on_delete=models.PROTECT, related_name="settlement_events")
    remote_event_id = models.CharField(max_length=255)
    state = models.CharField(max_length=24, choices=State.choices)
    gross = models.DecimalField(max_digits=14, decimal_places=2)
    fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    authoritative = models.BooleanField(default=False)
    evidence_source = models.CharField(max_length=120)
    evidence = models.JSONField(default=dict)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "remote_event_id"], name="uniq_inbound_settlement_event"),
            models.CheckConstraint(condition=models.Q(gross__gte=0), name="inbound_settlement_gross_nonnegative"),
            models.CheckConstraint(condition=models.Q(fee__gte=0) & models.Q(fee__lte=models.F("gross")), name="inbound_settlement_fee_valid"),
            models.CheckConstraint(condition=models.Q(net__gte=0), name="inbound_settlement_net_nonnegative"),
        ]


class FeePolicy(Timestamped):
    marketplace = models.ForeignKey(Marketplace, on_delete=models.PROTECT, related_name="fee_policies")
    policy_type = models.CharField(max_length=40, default="PAYMENT")
    currency = models.CharField(max_length=3, default="USD")
    percentage_rate = models.DecimalField(max_digits=8, decimal_places=6)
    fixed_fee = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    tax_rate = models.DecimalField(max_digits=8, decimal_places=6, default=0)
    source_url = models.URLField()
    source_version = models.CharField(max_length=80)
    effective_at = models.DateTimeField()
    verified = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["marketplace", "policy_type", "currency", "source_version"], name="uniq_market_fee_policy_version"),
            models.CheckConstraint(condition=models.Q(percentage_rate__gte=0) & models.Q(percentage_rate__lt=1), name="fee_policy_rate_valid"),
            models.CheckConstraint(condition=models.Q(fixed_fee__gte=0), name="fee_policy_fixed_nonnegative"),
            models.CheckConstraint(condition=models.Q(tax_rate__gte=0) & models.Q(tax_rate__lt=1), name="fee_policy_tax_valid"),
        ]


class CommercePayment(Timestamped):
    """Provider-neutral direct-commerce payment intent and authoritative result."""

    class State(models.TextChoices):
        CREATED = "CREATED"
        INITIALIZED = "INITIALIZED"
        PAID = "PAID"
        FAILED = "FAILED"
        REFUNDED = "REFUNDED"
        REVERSED = "REVERSED"
        UNKNOWN = "UNKNOWN"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    marketplace = models.ForeignKey(Marketplace, on_delete=models.PROTECT, related_name="commerce_payments")
    offering = models.ForeignKey(ServiceOffering, on_delete=models.PROTECT, related_name="commerce_payments")
    order = models.OneToOneField(InboundOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="commerce_payment")
    provider = models.CharField(max_length=80)
    external_reference = models.CharField(max_length=255)
    idempotency_key = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3, default="ZAR")
    customer_reference_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=24, choices=State.choices, default=State.CREATED)
    checkout_reference = models.CharField(max_length=700, blank=True)
    provider_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    authoritative = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    evidence = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "external_reference"], name="uniq_commerce_provider_reference"),
            models.UniqueConstraint(fields=["provider", "idempotency_key"], name="uniq_commerce_provider_idempotency"),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="commerce_payment_amount_positive"),
            models.CheckConstraint(condition=models.Q(provider_fee__gte=0) & models.Q(provider_fee__lte=models.F("amount")), name="commerce_payment_fee_valid"),
        ]


class OwnerReceipt(Timestamped):
    """Authoritative owner-controlled receipt, distinct from bank settlement."""

    class State(models.TextChoices):
        PLATFORM_WALLET = "PLATFORM_WALLET"
        PAYOUT_PENDING = "PAYOUT_PENDING"
        PAYSTACK_BALANCE = "PAYSTACK_BALANCE"
        PAYPAL_BALANCE = "PAYPAL_BALANCE"
        STABLECOIN_RECEIVED = "STABLECOIN_RECEIVED"
        CRYPTO_RECEIVED = "CRYPTO_RECEIVED"
        CONVERSION_PENDING = "CONVERSION_PENDING"
        FIAT_SETTLED = "FIAT_SETTLED"
        REVERSED = "REVERSED"

    marketplace = models.ForeignKey(Marketplace, on_delete=models.PROTECT, related_name="owner_receipts")
    payout = models.ForeignKey("Payout", null=True, blank=True, on_delete=models.PROTECT, related_name="owner_receipts")
    commerce_payment = models.ForeignKey(CommercePayment, null=True, blank=True, on_delete=models.PROTECT, related_name="owner_receipts")
    external_reference = models.CharField(max_length=255)
    rail = models.CharField(max_length=80)
    state = models.CharField(max_length=40, choices=State.choices)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=12, default="USD")
    authoritative = models.BooleanField(default=False)
    human_withdrawal_required = models.BooleanField(default=True)
    evidence = models.JSONField(default=dict)
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["marketplace", "external_reference", "state"], name="uniq_owner_receipt_state"),
            models.CheckConstraint(condition=models.Q(amount__gte=0), name="owner_receipt_amount_nonnegative"),
        ]


class PortfolioDecision(Timestamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey("Job", on_delete=models.CASCADE, related_name="portfolio_decisions")
    inbound_order = models.ForeignKey(InboundOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="portfolio_decisions")
    source_type = models.CharField(max_length=40)
    revenue_channel = models.CharField(max_length=40)
    rank = models.PositiveIntegerField()
    score = models.DecimalField(max_digits=18, decimal_places=6)
    selected = models.BooleanField(default=False)
    expected_net_profit = models.DecimalField(max_digits=14, decimal_places=2)
    risk_adjusted_profit = models.DecimalField(max_digits=14, decimal_places=2)
    profit_per_minute = models.DecimalField(max_digits=14, decimal_places=4)
    payout_probability = models.DecimalField(max_digits=7, decimal_places=6)
    acceptance_probability = models.DecimalField(max_digits=7, decimal_places=6)
    inputs = models.JSONField(default=dict)


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
    recommended_offer = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
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
    successful_executions = models.PositiveBigIntegerField(default=0)
    qa_accepted = models.PositiveBigIntegerField(default=0)
    qa_rejected = models.PositiveBigIntegerField(default=0)
    repair_required = models.PositiveBigIntegerField(default=0)
    failures = models.PositiveBigIntegerField(default=0)
    provider_failures = models.PositiveBigIntegerField(default=0)
    retry_count = models.PositiveBigIntegerField(default=0)
    deliverable_accepted = models.PositiveBigIntegerField(default=0)
    total_repair_cost = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    gross_profit = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    net_profit = models.DecimalField(max_digits=16, decimal_places=2, default=0)

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
    source_event_key = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "source_event_key"],
                condition=~models.Q(source_event_key=""),
                name="uniq_job_revision_source_event",
            )
        ]


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


class ResourceSnapshot(Timestamped):
    node_id = models.CharField(max_length=120, default="VPS1")
    purpose = models.CharField(max_length=40)
    disk_free_bytes = models.PositiveBigIntegerField(default=0)
    disk_free_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    memory_available_bytes = models.PositiveBigIntegerField(default=0)
    load_per_cpu = models.DecimalField(max_digits=8, decimal_places=3, default=0)
    storage_usage = models.JSONField(default=dict)
    queue_pressure = models.JSONField(default=dict)
    healthy = models.BooleanField(default=False)
    blocker_codes = models.JSONField(default=list)


class AdmissionDecision(Timestamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.SET_NULL, related_name="admission_decisions")
    snapshot = models.ForeignKey(ResourceSnapshot, null=True, blank=True, on_delete=models.SET_NULL)
    purpose = models.CharField(max_length=40)
    operation = models.CharField(max_length=80, blank=True)
    allowed = models.BooleanField(default=False)
    reason_codes = models.JSONField(default=list)
    details = models.JSONField(default=dict)


class AcquisitionPreflight(Timestamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="acquisition_preflights")
    autonomy_mode = models.CharField(max_length=16)
    operation = models.CharField(max_length=80, blank=True)
    worker_class = models.CharField(max_length=80, blank=True)
    eligible = models.BooleanField(default=False)
    allowed = models.BooleanField(default=False)
    reason_codes = models.JSONField(default=list)
    expected_gross = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    marketplace_fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    genx_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    operational_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    expected_net = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    confidence = models.DecimalField(max_digits=6, decimal_places=5, default=0)
    details = models.JSONField(default=dict)


class GrowthTarget(Timestamped):
    key = models.CharField(max_length=80, unique=True)
    period = models.CharField(max_length=24, default="DAILY")
    target_value = models.DecimalField(max_digits=18, decimal_places=4)
    unit = models.CharField(max_length=32)
    enabled = models.BooleanField(default=True)
    details = models.JSONField(default=dict)


class GrowthEvaluation(Timestamped):
    status = models.CharField(max_length=32)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    reason_codes = models.JSONField(default=list)
    metrics = models.JSONField(default=dict)
    targets = models.JSONField(default=dict)
    adjustments = models.JSONField(default=list)


class PerformanceAggregate(Timestamped):
    dimension_type = models.CharField(max_length=24)
    dimension_key = models.CharField(max_length=240)
    marketplace = models.ForeignKey(Marketplace, null=True, blank=True, on_delete=models.SET_NULL)
    capability = models.CharField(max_length=80, blank=True)
    operation = models.CharField(max_length=80, blank=True)
    worker_class = models.CharField(max_length=80, blank=True)
    worker_version = models.CharField(max_length=40, blank=True)
    strategy_key = models.CharField(max_length=120, blank=True)
    growth_stage = models.CharField(max_length=16, default="BOOTSTRAP")
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    jobs_discovered = models.PositiveIntegerField(default=0)
    jobs_attempted = models.PositiveIntegerField(default=0)
    jobs_awarded = models.PositiveIntegerField(default=0)
    jobs_completed = models.PositiveIntegerField(default=0)
    qa_first_pass_rate = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    repair_rate = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    revision_rate = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    on_time_rate = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    acceptance_rate = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    settlement_rate = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    gross_payout = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    platform_fees = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    genx_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    direct_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    expected_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    actual_cost = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    settled_profit = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    runtime_seconds = models.PositiveBigIntegerField(default=0)
    time_to_award_seconds = models.PositiveBigIntegerField(default=0)
    time_to_acceptance_seconds = models.PositiveBigIntegerField(default=0)
    time_to_settlement_seconds = models.PositiveBigIntegerField(default=0)
    profit_per_execution_minute = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    profit_per_genx_credit = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    reputation_delta = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    sample_count = models.PositiveIntegerField(default=0)
    details = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["dimension_type", "dimension_key", "window_start", "window_end"],
                name="uniq_performance_dimension_window",
            )
        ]


class ReputationSnapshot(Timestamped):
    marketplace = models.ForeignKey(Marketplace, on_delete=models.CASCADE, related_name="reputation_snapshots")
    capability = models.CharField(max_length=80, blank=True)
    rating = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    rating_count = models.PositiveIntegerField(default=0)
    completed_jobs = models.PositiveIntegerField(default=0)
    revision_rate = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    on_time_rate = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    source = models.CharField(max_length=120)
    observed_at = models.DateTimeField(default=timezone.now)
    details = models.JSONField(default=dict)


class CapacitySnapshot(Timestamped):
    node_id = models.CharField(max_length=120, default="VPS1")
    productive_slots = models.PositiveIntegerField(default=0)
    active_slots = models.PositiveIntegerField(default=0)
    available_slots = models.PositiveIntegerField(default=0)
    reserved_slots = models.PositiveIntegerField(default=0)
    utilization = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    utilization_state = models.CharField(max_length=24)
    profitable_eligible_waiting = models.PositiveIntegerField(default=0)
    avoidable_idle_minutes = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    unavoidable_idle_minutes = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    estimated_foregone_profit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    idle_reason = models.CharField(max_length=120, blank=True)
    details = models.JSONField(default=dict)


class PricingStrategy(Timestamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    marketplace = models.ForeignKey(Marketplace, on_delete=models.CASCADE, related_name="pricing_strategies")
    capability = models.CharField(max_length=80)
    operation = models.CharField(max_length=80)
    growth_stage = models.CharField(max_length=16)
    utilization_state = models.CharField(max_length=24)
    minimum_profitable_price = models.DecimalField(max_digits=14, decimal_places=2)
    advertised_budget = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    competitive_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    offered_price = models.DecimalField(max_digits=14, decimal_places=2)
    desired_margin = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    exploration = models.BooleanField(default=False)
    adjustment_fraction = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    outcome = models.CharField(max_length=32, default="PENDING")
    reason_codes = models.JSONField(default=list)
    details = models.JSONField(default=dict)


class OpportunityDecision(Timestamped):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="opportunity_decisions")
    preflight = models.ForeignKey(AcquisitionPreflight, null=True, blank=True, on_delete=models.SET_NULL)
    capacity = models.ForeignKey(CapacitySnapshot, null=True, blank=True, on_delete=models.SET_NULL)
    pricing_strategy = models.ForeignKey(PricingStrategy, null=True, blank=True, on_delete=models.SET_NULL)
    growth_stage = models.CharField(max_length=16)
    utilization_state = models.CharField(max_length=24)
    allowed = models.BooleanField(default=False)
    exploration = models.BooleanField(default=False)
    reputation_investment = models.BooleanField(default=False)
    expected_cash_profit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    risk_adjusted_profit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    reputation_contribution = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    learning_contribution = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    opportunity_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    resource_minutes = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    time_to_cash_hours = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    reason_codes = models.JSONField(default=list)
    details = models.JSONField(default=dict)


class StrategyAdjustment(Timestamped):
    scope_type = models.CharField(max_length=24)
    scope_key = models.CharField(max_length=240)
    growth_stage = models.CharField(max_length=16)
    adjustment_type = models.CharField(max_length=80)
    previous_value = models.JSONField(default=dict)
    proposed_value = models.JSONField(default=dict)
    reason_codes = models.JSONField(default=list)
    bounded = models.BooleanField(default=True)
    applied = models.BooleanField(default=False)


class SyntheticDatasetRun(Timestamped):
    class Mode(models.TextChoices):
        COMMISSIONED = "COMMISSIONED"
        INVENTORY = "INVENTORY"

    class Status(models.TextChoices):
        COMPLETED = "COMPLETED"
        REJECTED = "REJECTED"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="synthetic_dataset_runs")
    execution = models.OneToOneField(Execution, on_delete=models.CASCADE, related_name="synthetic_dataset_run")
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.COMMISSIONED)
    status = models.CharField(max_length=20, choices=Status.choices)
    schema = models.JSONField(default=dict)
    generation_plan = models.JSONField(default=dict)
    provenance = models.JSONField(default=dict)
    rights_confirmed = models.BooleanField(default=False)
    demand_evidence = models.JSONField(default=dict)
    budget_authorized = models.BooleanField(default=False)
    requested_records = models.PositiveIntegerField(default=0)
    records_generated = models.PositiveIntegerField(default=0)
    accepted_records = models.PositiveIntegerField(default=0)
    duplicate_records = models.PositiveIntegerField(default=0)
    invalid_records = models.PositiveIntegerField(default=0)
    class_distribution = models.JSONField(default=dict)
    split_counts = models.JSONField(default=dict)
    generation_cost = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    genx_credits = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    cost_per_accepted_record = models.DecimalField(max_digits=18, decimal_places=8, default=0)
    qa_rejection_rate = models.DecimalField(max_digits=7, decimal_places=6, default=0)
    artifact_manifest = models.JSONField(default=dict)
    reason_codes = models.JSONField(default=list)


class BountyProgram(Timestamped):
    class Status(models.TextChoices):
        DRAFT = "DRAFT"
        ACTIVE = "ACTIVE"
        EXPIRED = "EXPIRED"
        REVOKED = "REVOKED"

    name = models.CharField(max_length=200)
    provider = models.CharField(max_length=120)
    external_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    execution_enabled = models.BooleanField(default=False)
    automation_allowed = models.BooleanField(default=False)
    terms_url = models.URLField(blank=True)
    authorization_source = models.TextField(blank=True)
    details = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "external_id"], condition=~models.Q(external_id=""), name="uniq_bounty_program_external_id")
        ]


class ProgramScopeVersion(Timestamped):
    program = models.ForeignKey(BountyProgram, on_delete=models.CASCADE, related_name="scope_versions")
    version = models.PositiveIntegerField()
    authorization_hash = models.CharField(max_length=64)
    rules_snapshot = models.JSONField(default=dict)
    allowed_test_types = models.JSONField(default=list)
    prohibited_test_types = models.JSONField(default=list)
    rate_limit_per_minute = models.PositiveIntegerField(default=0)
    max_requests_per_attempt = models.PositiveIntegerField(default=0)
    max_spend_per_attempt = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    effective_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    active = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["program", "version"], name="uniq_bounty_program_scope_version")]


class AuthorizedTarget(Timestamped):
    class TargetType(models.TextChoices):
        SUPPLIED_SANDBOX = "SUPPLIED_SANDBOX"
        LOCAL_FIXTURE = "LOCAL_FIXTURE"
        REMOTE_PROGRAM = "REMOTE_PROGRAM"

    scope_version = models.ForeignKey(ProgramScopeVersion, on_delete=models.CASCADE, related_name="authorized_targets")
    target_type = models.CharField(max_length=24, choices=TargetType.choices)
    canonical_target = models.CharField(max_length=500)
    authorization_evidence = models.TextField()
    network_access_allowed = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["scope_version", "canonical_target"], name="uniq_authorized_scope_target")]


class SafetyResearchAttempt(Timestamped):
    class Status(models.TextChoices):
        BLOCKED = "BLOCKED"
        AUTHORIZED = "AUTHORIZED"
        RUNNING = "RUNNING"
        COMPLETED = "COMPLETED"
        FAILED = "FAILED"

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="safety_research_attempts")
    program = models.ForeignKey(BountyProgram, on_delete=models.PROTECT, related_name="attempts")
    scope_version = models.ForeignKey(ProgramScopeVersion, on_delete=models.PROTECT, related_name="attempts")
    target = models.ForeignKey(AuthorizedTarget, on_delete=models.PROTECT, related_name="attempts")
    execution = models.OneToOneField(Execution, null=True, blank=True, on_delete=models.SET_NULL, related_name="safety_research_attempt")
    test_type = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=Status.choices)
    plan = models.JSONField(default=dict)
    authorization_snapshot = models.JSONField(default=dict)
    estimated_spend = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    max_requests = models.PositiveIntegerField(default=0)
    executed_requests = models.PositiveIntegerField(default=0)
    reason_codes = models.JSONField(default=list)
    result = models.JSONField(default=dict)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)


class SafetyFinding(Timestamped):
    class Status(models.TextChoices):
        CANDIDATE = "CANDIDATE"
        REPRODUCED = "REPRODUCED"
        NOT_REPRODUCED = "NOT_REPRODUCED"
        SUBMISSION_READY = "SUBMISSION_READY"
        SUBMITTED = "SUBMITTED"
        REJECTED = "REJECTED"

    attempt = models.ForeignKey(SafetyResearchAttempt, on_delete=models.CASCADE, related_name="findings")
    fingerprint = models.CharField(max_length=64)
    title = models.CharField(max_length=300)
    impact = models.TextField()
    severity = models.CharField(max_length=24)
    evidence = models.JSONField(default=dict)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.CANDIDATE)
    duplicate_checked = models.BooleanField(default=False)
    contains_private_data = models.BooleanField(default=False)
    sanitized = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["attempt", "fingerprint"], name="uniq_safety_finding_attempt_fingerprint")]


class FindingReproduction(Timestamped):
    finding = models.ForeignKey(SafetyFinding, on_delete=models.CASCADE, related_name="reproductions")
    independent_reviewer = models.CharField(max_length=160)
    reproduced = models.BooleanField(default=False)
    evidence = models.JSONField(default=dict)
    evidence_hash = models.CharField(max_length=64)
    reproduced_at = models.DateTimeField(default=timezone.now)


class SafetyBountySubmission(Timestamped):
    class Status(models.TextChoices):
        DRAFT = "DRAFT"
        SUBMITTED = "SUBMITTED"
        TRIAGED = "TRIAGED"
        CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
        AWARDED = "AWARDED"
        REJECTED = "REJECTED"

    finding = models.OneToOneField(SafetyFinding, on_delete=models.PROTECT, related_name="bounty_submission")
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.DRAFT)
    submission_method = models.CharField(max_length=120)
    remote_reference = models.CharField(max_length=255, blank=True)
    report_artifact = models.ForeignKey(Artifact, null=True, blank=True, on_delete=models.SET_NULL)
    awarded_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    submitted_at = models.DateTimeField(null=True, blank=True)
    triage = models.JSONField(default=dict)


class SanitizedEvaluationCase(Timestamped):
    finding = models.ForeignKey(SafetyFinding, on_delete=models.PROTECT, related_name="sanitized_evaluation_cases")
    case_type = models.CharField(max_length=80)
    prompt = models.TextField()
    expected_behavior = models.TextField()
    provenance = models.JSONField(default=dict)
    rights_confirmed = models.BooleanField(default=False)
    private_data_removed = models.BooleanField(default=False)
    harmful_detail_removed = models.BooleanField(default=False)


class ServiceHeartbeat(Timestamped):
    service = models.CharField(max_length=80, unique=True)
    node_id = models.CharField(max_length=120, default="VPS1")
    last_seen_at = models.DateTimeField(default=timezone.now)
    details = models.JSONField(default=dict)


class RecoveryAction(Timestamped):
    action_key = models.CharField(max_length=200, unique=True)
    target_type = models.CharField(max_length=80)
    target_id = models.CharField(max_length=160, blank=True)
    action = models.CharField(max_length=120)
    outcome = models.CharField(max_length=32)
    reason_code = models.CharField(max_length=120)
    details = models.JSONField(default=dict)
    performed_at = models.DateTimeField(default=timezone.now)


class WebhookEvent(Timestamped):
    marketplace = models.ForeignKey(Marketplace, on_delete=models.CASCADE, related_name="webhook_events")
    event_key = models.CharField(max_length=64, unique=True)
    event_type = models.CharField(max_length=120)
    external_job_id = models.CharField(max_length=255, blank=True, db_index=True)
    occurred_at_remote = models.CharField(max_length=80, blank=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=32, default="RECEIVED")
    attempt_count = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=120, blank=True)
    external_event_id = models.CharField(max_length=255, blank=True, db_index=True)
    signature_valid = models.BooleanField(null=True, blank=True)
    raw_body_hash = models.CharField(max_length=64, blank=True)
    retryable = models.BooleanField(default=False)
    unknown_external_state = models.BooleanField(default=False)
    inbound_order = models.ForeignKey(InboundOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="external_events")
    commerce_payment = models.ForeignKey(CommercePayment, null=True, blank=True, on_delete=models.PROTECT, related_name="external_events")


class IntegrationProofRun(Timestamped):
    """Append-only stage evidence for bounded live proving; never enables autonomy."""

    class State(models.TextChoices):
        PENDING = "PENDING"
        PASSED = "PASSED"
        FAILED = "FAILED"
        BLOCKED = "BLOCKED"

    marketplace = models.ForeignKey(Marketplace, on_delete=models.PROTECT, related_name="proof_runs")
    stage = models.CharField(max_length=80)
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    authoritative = models.BooleanField(default=False)
    evidence_reference = models.CharField(max_length=255, blank=True)
    safe_detail = models.CharField(max_length=500, blank=True)
    performed_by = models.CharField(max_length=120, blank=True)


class AffiliateProgram(Timestamped):
    marketplace = models.ForeignKey(Marketplace, on_delete=models.PROTECT, related_name="affiliate_programs")
    external_program_id = models.CharField(max_length=255)
    display_name = models.CharField(max_length=200)
    destination = models.URLField(blank=True)
    disclosure_required = models.BooleanField(default=True)
    status = models.CharField(max_length=40, default="MANUAL_PUBLICATION")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["marketplace", "external_program_id"], name="uniq_affiliate_program")]


class AffiliateCommission(Timestamped):
    class State(models.TextChoices):
        PENDING = "PENDING"
        APPROVED = "APPROVED"
        PAYABLE = "PAYABLE"
        PAID = "PAID"
        REVERSED = "REVERSED"

    program = models.ForeignKey(AffiliateProgram, on_delete=models.PROTECT, related_name="commissions")
    external_conversion_id = models.CharField(max_length=255)
    state = models.CharField(max_length=20, choices=State.choices)
    gross = models.DecimalField(max_digits=14, decimal_places=2)
    fee = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    attributable_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="USD")
    authoritative = models.BooleanField(default=False)
    evidence = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["program", "external_conversion_id"], name="uniq_affiliate_conversion"),
            models.CheckConstraint(condition=models.Q(gross__gte=0), name="affiliate_gross_nonnegative"),
        ]


class ProductCandidate(Timestamped):
    class State(models.TextChoices):
        PRODUCT_CANDIDATE = "PRODUCT_CANDIDATE"
        ECONOMICS_APPROVED = "ECONOMICS_APPROVED"
        ASSET_BUILT = "ASSET_BUILT"
        QA_PASSED = "QA_PASSED"
        READY_TO_PUBLISH = "READY_TO_PUBLISH"
        PUBLISHED = "PUBLISHED"
        PAUSED = "PAUSED"
        RETIRED = "RETIRED"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True)
    product_class = models.CharField(max_length=60)
    title = models.CharField(max_length=200)
    target_buyer = models.CharField(max_length=300)
    currency = models.CharField(max_length=3, default="USD")
    offering = models.ForeignKey(ServiceOffering, null=True, blank=True, on_delete=models.PROTECT, related_name="product_candidates")
    job = models.OneToOneField(Job, null=True, blank=True, on_delete=models.PROTECT, related_name="product_candidate")
    state = models.CharField(max_length=32, choices=State.choices, default=State.PRODUCT_CANDIDATE)
    intended_channels = models.JSONField(default=list)
    suggested_price = models.DecimalField(max_digits=14, decimal_places=2)
    expected_sales = models.PositiveIntegerField(default=1)
    expected_gross = models.DecimalField(max_digits=16, decimal_places=2)
    expected_cost = models.DecimalField(max_digits=16, decimal_places=4)
    expected_net = models.DecimalField(max_digits=16, decimal_places=2)
    expected_margin = models.DecimalField(max_digits=8, decimal_places=6)
    confidence = models.DecimalField(max_digits=7, decimal_places=6)
    max_genx_credits = models.DecimalField(max_digits=16, decimal_places=4)
    max_inventory_quantity = models.PositiveIntegerField(default=1)
    inventory_quantity = models.PositiveIntegerField(default=0)
    commercial_copy = models.JSONField(default=dict)
    rights_evidence = models.JSONField(default=dict)
    qa_evidence = models.JSONField(default=dict)
    cost_basis = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    publication_cost = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    promotion_cost = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    impressions = models.PositiveBigIntegerField(default=0)
    clicks = models.PositiveBigIntegerField(default=0)
    sales = models.PositiveIntegerField(default=0)
    refunds = models.PositiveIntegerField(default=0)
    gross_revenue = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    payout_received = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    net_profit = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    return_on_production_cost = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    first_sale_at = models.DateTimeField(null=True, blank=True)
    break_even_at = models.DateTimeField(null=True, blank=True)
    commercial_evidence = models.JSONField(default=dict)
    review_at = models.DateTimeField(null=True, blank=True)
    paused_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(suggested_price__gt=0), name="product_price_positive"),
            models.CheckConstraint(condition=models.Q(expected_cost__gte=0), name="product_expected_cost_nonnegative"),
            models.CheckConstraint(condition=models.Q(max_genx_credits__gte=0), name="product_genx_budget_nonnegative"),
        ]


class InternalOpportunity(Timestamped):
    class State(models.TextChoices):
        CANDIDATE = "CANDIDATE"
        ECONOMICS_APPROVED = "ECONOMICS_APPROVED"
        QUEUED = "QUEUED"
        EXECUTING = "EXECUTING"
        COMPLETED = "COMPLETED"
        BLOCKED = "BLOCKED"

    product = models.ForeignKey(ProductCandidate, on_delete=models.CASCADE, related_name="opportunities")
    job = models.OneToOneField(Job, null=True, blank=True, on_delete=models.PROTECT, related_name="internal_opportunity")
    opportunity_type = models.CharField(max_length=60)
    priority = models.PositiveSmallIntegerField(default=80)
    expected_value = models.DecimalField(max_digits=16, decimal_places=2)
    state = models.CharField(max_length=24, choices=State.choices, default=State.CANDIDATE)
    reason_codes = models.JSONField(default=list)
    deduplication_key = models.CharField(max_length=160, unique=True)


class CapabilityMonetization(Timestamped):
    worker_class = models.CharField(max_length=80)
    operation = models.CharField(max_length=80)
    genx_task_class = models.CharField(max_length=100, blank=True)
    commercial_deliverable = models.CharField(max_length=200)
    offering = models.ForeignKey(ServiceOffering, null=True, blank=True, on_delete=models.PROTECT, related_name="capability_mappings")
    channels = models.JSONField(default=list)
    expected_price = models.DecimalField(max_digits=14, decimal_places=2)
    estimated_cost = models.DecimalField(max_digits=14, decimal_places=4)
    expected_margin = models.DecimalField(max_digits=8, decimal_places=6)
    readiness = models.CharField(max_length=40, default="CANDIDATE")
    input_schema = models.JSONField(default=dict)
    output_schema = models.JSONField(default=dict)
    qa_profile = models.CharField(max_length=80)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["worker_class", "operation", "commercial_deliverable"], name="uniq_capability_monetization")]


class DistributionCampaign(Timestamped):
    product = models.ForeignKey(ProductCandidate, on_delete=models.PROTECT, related_name="campaigns")
    channel = models.CharField(max_length=80)
    disclosure = models.TextField(blank=True)
    tracking_reference = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=40, default="OWNER_REVIEW")
    cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    attributed_revenue = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    impressions = models.PositiveBigIntegerField(default=0)
    clicks = models.PositiveBigIntegerField(default=0)
    conversions = models.PositiveIntegerField(default=0)
    content_assets = models.JSONField(default=list)


class AuditEvent(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    severity = models.CharField(max_length=16, default="INFO")
    event_type = models.CharField(max_length=120)
    actor = models.CharField(max_length=120, blank=True)
    correlation_id = models.UUIDField(default=uuid.uuid4)
    metadata = models.JSONField(default=dict)
