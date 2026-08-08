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
    source = models.CharField(max_length=40, default="upload")
    external_id = models.CharField(max_length=255, blank=True)
    name = models.CharField(max_length=255)
    path = models.CharField(max_length=700, blank=True)
    url = models.URLField(blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True)
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
