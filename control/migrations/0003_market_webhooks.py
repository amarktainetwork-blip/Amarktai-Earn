import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("control", "0002_runtime_economics_genx")]

    operations = [
        migrations.AddField(
            model_name="jobscore",
            name="recommended_offer",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="revision",
            name="source_event_key",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="revision",
            constraint=models.UniqueConstraint(
                condition=~models.Q(source_event_key=""),
                fields=("job", "source_event_key"),
                name="uniq_job_revision_source_event",
            ),
        ),
        migrations.CreateModel(
            name="WebhookEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("event_key", models.CharField(max_length=64, unique=True)),
                ("event_type", models.CharField(max_length=120)),
                ("external_job_id", models.CharField(blank=True, db_index=True, max_length=255)),
                ("occurred_at_remote", models.CharField(blank=True, max_length=80)),
                ("payload", models.JSONField(default=dict)),
                ("status", models.CharField(default="RECEIVED", max_length=32)),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                ("last_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, max_length=120)),
                ("marketplace", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="webhook_events", to="control.marketplace")),
            ],
        ),
    ]
