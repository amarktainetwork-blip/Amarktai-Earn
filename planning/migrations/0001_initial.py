import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("control", "0004_schema_drift_alignment")]

    operations = [
        migrations.CreateModel(
            name="JobAsset",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("source", models.CharField(default="upload", max_length=40)),
                ("external_id", models.CharField(blank=True, max_length=255)),
                ("name", models.CharField(max_length=255)),
                ("path", models.CharField(blank=True, max_length=700)),
                ("url", models.URLField(blank=True)),
                ("sha256", models.CharField(blank=True, max_length=64)),
                ("size_bytes", models.PositiveBigIntegerField(default=0)),
                ("mime_type", models.CharField(blank=True, max_length=120)),
                ("status", models.CharField(choices=[("STAGED", "Staged"), ("VERIFIED", "Verified"), ("BLOCKED", "Blocked")], default="STAGED", max_length=32)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assets", to="control.job")),
            ],
        ),
        migrations.CreateModel(
            name="WorkPlan",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("worker_class", models.CharField(blank=True, max_length=80)),
                ("operation", models.CharField(blank=True, max_length=80)),
                ("input_spec", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("BLOCKED", "Blocked"), ("READY", "Ready"), ("QUEUED", "Queued"), ("EXECUTING", "Executing"), ("NEEDS_REPAIR", "Needs Repair"), ("QA_PASSED", "Qa Passed"), ("SUBMITTING", "Submitting"), ("SUBMISSION_RECONCILIATION", "Submission Reconciliation"), ("SUBMITTED", "Submitted"), ("FAILED", "Failed")], default="BLOCKED", max_length=32)),
                ("planner_version", models.CharField(default="deterministic-v1", max_length=32)),
                ("reason_codes", models.JSONField(default=list)),
                ("execution_attempts", models.PositiveSmallIntegerField(default=0)),
                ("repair_attempts", models.PositiveSmallIntegerField(default=0)),
                ("max_repair_attempts", models.PositiveSmallIntegerField(default=1)),
                ("last_error_code", models.CharField(blank=True, max_length=120)),
                ("last_queued_at", models.DateTimeField(blank=True, null=True)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("job", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="work_plan", to="control.job")),
            ],
        ),
        migrations.AddConstraint(
            model_name="jobasset",
            constraint=models.UniqueConstraint(condition=~models.Q(external_id=""), fields=("job", "external_id"), name="uniq_job_asset_external_id"),
        ),
    ]
