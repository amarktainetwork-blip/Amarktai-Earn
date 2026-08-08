import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("control", "0005_operations_governor"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="AuthThrottle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("key_hash", models.CharField(max_length=64, unique=True)),
                ("scope", models.CharField(max_length=40)),
                ("failure_count", models.PositiveIntegerField(default=0)),
                ("window_started_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_failure_at", models.DateTimeField(blank=True, null=True)),
                ("locked_until", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="ReauthenticationGrant",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("allowed_actions", models.JSONField(default=list)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="AcquisitionPreflight",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("autonomy_mode", models.CharField(max_length=16)),
                ("operation", models.CharField(blank=True, max_length=80)),
                ("worker_class", models.CharField(blank=True, max_length=80)),
                ("eligible", models.BooleanField(default=False)),
                ("allowed", models.BooleanField(default=False)),
                ("reason_codes", models.JSONField(default=list)),
                ("expected_gross", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("marketplace_fee", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("genx_cost", models.DecimalField(decimal_places=4, default=0, max_digits=14)),
                ("operational_cost", models.DecimalField(decimal_places=4, default=0, max_digits=14)),
                ("expected_net", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("confidence", models.DecimalField(decimal_places=5, default=0, max_digits=6)),
                ("details", models.JSONField(default=dict)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="acquisition_preflights", to="control.job")),
            ],
        ),
    ]
