import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("planning", "0002_repositorysnapshot"), ("control", "0006_security_autonomy_preflight")]

    operations = [
        migrations.CreateModel(
            name="DependencyPreparation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("ecosystem", models.CharField(max_length=20)),
                ("manifest_path", models.CharField(max_length=255)),
                ("manifest_hash", models.CharField(max_length=64)),
                ("cache_key", models.CharField(blank=True, max_length=100)),
                ("status", models.CharField(choices=[("REQUESTED", "Requested"), ("READY", "Ready"), ("BLOCKED", "Blocked"), ("FAILED", "Failed")], default="REQUESTED", max_length=20)),
                ("file_count", models.PositiveIntegerField(default=0)),
                ("total_bytes", models.PositiveBigIntegerField(default=0)),
                ("reason_codes", models.JSONField(default=list)),
                ("details", models.JSONField(default=dict)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dependency_preparations", to="control.job")),
                ("repository_snapshot", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="dependency_preparations", to="planning.repositorysnapshot")),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("repository_snapshot", "ecosystem", "manifest_hash"), name="uniq_dependency_snapshot_manifest")],
            },
        ),
    ]
