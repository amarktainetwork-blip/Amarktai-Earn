from django.db import models


class Timestamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class JobAsset(Timestamped):
    class Status(models.TextChoices):
        STAGED = "STAGED"
        VERIFIED = "VERIFIED"
        BLOCKED = "BLOCKED"

    job = models.ForeignKey("control.Job", on_delete=models.CASCADE, related_name="assets")
    semantic_role = models.CharField(max_length=40, default="source")
    source = models.CharField(max_length=40, default="upload")
    external_id = models.CharField(max_length=255, blank=True)
    name = models.CharField(max_length=255)
    path = models.CharField(max_length=700, blank=True)
    url = models.URLField(blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True)
    declared_mime_type = models.CharField(max_length=120, blank=True)
    detected_mime_type = models.CharField(max_length=120, blank=True)
    duplicate_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates")
    archive_inspected = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.STAGED)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "external_id"],
                condition=~models.Q(external_id=""),
                name="uniq_job_asset_external_id",
            )
        ]


class JobAssetManifest(Timestamped):
    class Status(models.TextChoices):
        VERIFIED = "VERIFIED"
        BLOCKED = "BLOCKED"

    job = models.OneToOneField("control.Job", on_delete=models.CASCADE, related_name="asset_manifest")
    version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.BLOCKED)
    file_count = models.PositiveIntegerField(default=0)
    total_bytes = models.PositiveBigIntegerField(default=0)
    manifest_sha256 = models.CharField(max_length=64, blank=True)
    roles = models.JSONField(default=dict)
    reason_codes = models.JSONField(default=list)
    verified_at = models.DateTimeField(null=True, blank=True)


class WorkPlan(Timestamped):
    class Status(models.TextChoices):
        BLOCKED = "BLOCKED"
        READY = "READY"
        QUEUED = "QUEUED"
        EXECUTING = "EXECUTING"
        NEEDS_REPAIR = "NEEDS_REPAIR"
        QA_PASSED = "QA_PASSED"
        SUBMITTING = "SUBMITTING"
        SUBMISSION_RECONCILIATION = "SUBMISSION_RECONCILIATION"
        SUBMITTED = "SUBMITTED"
        FAILED = "FAILED"

    job = models.OneToOneField("control.Job", on_delete=models.CASCADE, related_name="work_plan")
    worker_class = models.CharField(max_length=80, blank=True)
    operation = models.CharField(max_length=80, blank=True)
    input_spec = models.JSONField(default=dict)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.BLOCKED)
    planner_version = models.CharField(max_length=32, default="deterministic-v1")
    reason_codes = models.JSONField(default=list)
    execution_attempts = models.PositiveSmallIntegerField(default=0)
    repair_attempts = models.PositiveSmallIntegerField(default=0)
    max_repair_attempts = models.PositiveSmallIntegerField(default=1)
    last_error_code = models.CharField(max_length=120, blank=True)
    last_queued_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    is_composite = models.BooleanField(default=False)
    max_steps = models.PositiveSmallIntegerField(default=1)
    minimum_quality = models.DecimalField(max_digits=6, decimal_places=5, default=0.80)
    max_repair_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    escalation_policy = models.JSONField(default=dict)


class AcceptanceContract(Timestamped):
    """Versioned, source-grounded definition of what makes a job deliverable acceptable."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE"
        STALE = "STALE"
        BLOCKED = "BLOCKED"

    job = models.ForeignKey("control.Job", on_delete=models.CASCADE, related_name="acceptance_contracts")
    version = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    is_current = models.BooleanField(default=True)
    source_hash = models.CharField(max_length=64)
    compiler_version = models.CharField(max_length=40, default="acceptance-compiler-v1")
    source_requirements = models.JSONField(default=dict)
    compiled_task = models.JSONField(default=dict)
    criteria = models.JSONField(default=list)
    reason_codes = models.JSONField(default=list)

    class Meta:
        ordering = ["-version", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["job", "version"], name="uniq_acceptance_contract_job_version"),
            models.UniqueConstraint(
                fields=["job"],
                condition=models.Q(is_current=True),
                name="uniq_current_acceptance_contract_per_job",
            ),
        ]


class AcceptanceEvaluation(Timestamped):
    """Canonical submission-gate decision; semantic review cannot override deterministic failure."""

    class SemanticState(models.TextChoices):
        PASS = "PASS"
        FAIL = "FAIL"
        UNCERTAIN = "UNCERTAIN"

    contract = models.ForeignKey(AcceptanceContract, on_delete=models.PROTECT, related_name="evaluations")
    execution = models.OneToOneField(
        "control.Execution", on_delete=models.CASCADE, related_name="acceptance_evaluation"
    )
    evaluator_version = models.CharField(max_length=40, default="acceptance-evaluator-v1")
    deterministic_passed = models.BooleanField(default=False)
    semantic_state = models.CharField(
        max_length=16, choices=SemanticState.choices, default=SemanticState.UNCERTAIN
    )
    submission_ready = models.BooleanField(default=False)
    criterion_results = models.JSONField(default=list)
    critical_failures = models.JSONField(default=list)
    evidence = models.JSONField(default=dict)



class WorkPlanStep(Timestamped):
    class Status(models.TextChoices):
        BLOCKED = "BLOCKED"
        READY = "READY"
        EXECUTING = "EXECUTING"
        NEEDS_REPAIR = "NEEDS_REPAIR"
        QA_PASSED = "QA_PASSED"
        FAILED = "FAILED"

    plan = models.ForeignKey(WorkPlan, on_delete=models.CASCADE, related_name="steps")
    key = models.SlugField(max_length=80)
    sequence = models.PositiveSmallIntegerField()
    operation = models.CharField(max_length=80)
    worker_class = models.CharField(max_length=80)
    input_spec = models.JSONField(default=dict)
    input_assets = models.ManyToManyField(JobAsset, blank=True, related_name="workplan_steps")
    input_artifacts = models.ManyToManyField("control.Artifact", blank=True, related_name="downstream_steps")
    output_artifacts = models.ManyToManyField("control.Artifact", blank=True, related_name="producing_steps")
    execution = models.ForeignKey("control.Execution", null=True, blank=True, on_delete=models.SET_NULL, related_name="workplan_steps")
    qa_result = models.ForeignKey("control.QAResult", null=True, blank=True, on_delete=models.SET_NULL, related_name="workplan_steps")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.BLOCKED)
    attempt = models.PositiveSmallIntegerField(default=0)
    repair_attempts = models.PositiveSmallIntegerField(default=0)
    max_repair_attempts = models.PositiveSmallIntegerField(default=1)
    estimated_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    actual_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    repair_history = models.JSONField(default=list)
    reason_codes = models.JSONField(default=list)
    minimum_quality = models.DecimalField(max_digits=6, decimal_places=5, default=0.80)
    max_repair_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)

    class Meta:
        ordering = ["sequence", "id"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "key"], name="uniq_workplan_step_key"),
            models.UniqueConstraint(fields=["plan", "sequence"], name="uniq_workplan_step_sequence"),
        ]


class WorkPlanStepDependency(models.Model):
    step = models.ForeignKey(WorkPlanStep, on_delete=models.CASCADE, related_name="dependency_links")
    depends_on = models.ForeignKey(WorkPlanStep, on_delete=models.CASCADE, related_name="dependent_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["step", "depends_on"], name="uniq_workplan_step_dependency"),
            models.CheckConstraint(condition=~models.Q(step=models.F("depends_on")), name="workplan_step_no_self_dependency"),
        ]


class RepositorySnapshot(Timestamped):
    class Status(models.TextChoices):
        STAGED = "STAGED"
        VERIFIED = "VERIFIED"
        BLOCKED = "BLOCKED"

    job = models.OneToOneField("control.Job", on_delete=models.CASCADE, related_name="repository_snapshot")
    provider = models.CharField(max_length=40, default="github")
    repository_url = models.URLField()
    owner = models.CharField(max_length=120)
    repository = models.CharField(max_length=120)
    ref = models.CharField(max_length=255, blank=True)
    commit_sha = models.CharField(max_length=64, blank=True)
    path = models.CharField(max_length=700, blank=True)
    file_count = models.PositiveIntegerField(default=0)
    total_bytes = models.PositiveBigIntegerField(default=0)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.STAGED)
    error_code = models.CharField(max_length=120, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)


class DependencyPreparation(Timestamped):
    class Status(models.TextChoices):
        REQUESTED = "REQUESTED"
        READY = "READY"
        BLOCKED = "BLOCKED"
        FAILED = "FAILED"

    job = models.ForeignKey("control.Job", on_delete=models.CASCADE, related_name="dependency_preparations")
    repository_snapshot = models.ForeignKey(RepositorySnapshot, on_delete=models.CASCADE, related_name="dependency_preparations")
    ecosystem = models.CharField(max_length=20)
    manifest_path = models.CharField(max_length=255)
    manifest_hash = models.CharField(max_length=64)
    cache_key = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED)
    file_count = models.PositiveIntegerField(default=0)
    total_bytes = models.PositiveBigIntegerField(default=0)
    reason_codes = models.JSONField(default=list)
    details = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["repository_snapshot", "ecosystem", "manifest_hash"],
                name="uniq_dependency_snapshot_manifest",
            )
        ]
