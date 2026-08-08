import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("control", "0004_schema_drift_alignment")]

    operations = [
        migrations.CreateModel(
            name="ResourceSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("node_id", models.CharField(default="VPS1", max_length=120)),
                ("purpose", models.CharField(max_length=40)),
                ("disk_free_bytes", models.PositiveBigIntegerField(default=0)),
                ("disk_free_percent", models.DecimalField(decimal_places=2, default=0, max_digits=6)),
                ("memory_available_bytes", models.PositiveBigIntegerField(default=0)),
                ("load_per_cpu", models.DecimalField(decimal_places=3, default=0, max_digits=8)),
                ("storage_usage", models.JSONField(default=dict)),
                ("queue_pressure", models.JSONField(default=dict)),
                ("healthy", models.BooleanField(default=False)),
                ("blocker_codes", models.JSONField(default=list)),
            ],
        ),
        migrations.CreateModel(
            name="ServiceHeartbeat",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("service", models.CharField(max_length=80, unique=True)),
                ("node_id", models.CharField(default="VPS1", max_length=120)),
                ("last_seen_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("details", models.JSONField(default=dict)),
            ],
        ),
        migrations.CreateModel(
            name="RecoveryAction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("action_key", models.CharField(max_length=200, unique=True)),
                ("target_type", models.CharField(max_length=80)),
                ("target_id", models.CharField(blank=True, max_length=160)),
                ("action", models.CharField(max_length=120)),
                ("outcome", models.CharField(max_length=32)),
                ("reason_code", models.CharField(max_length=120)),
                ("details", models.JSONField(default=dict)),
                ("performed_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
        ),
        migrations.CreateModel(
            name="AdmissionDecision",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("purpose", models.CharField(max_length=40)),
                ("operation", models.CharField(blank=True, max_length=80)),
                ("allowed", models.BooleanField(default=False)),
                ("reason_codes", models.JSONField(default=list)),
                ("details", models.JSONField(default=dict)),
                ("job", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="admission_decisions", to="control.job")),
                ("snapshot", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="control.resourcesnapshot")),
            ],
        ),
    ]
