from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("planning", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="RepositorySnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("provider", models.CharField(default="github", max_length=40)),
                ("repository_url", models.URLField()),
                ("owner", models.CharField(max_length=120)),
                ("repository", models.CharField(max_length=120)),
                ("ref", models.CharField(blank=True, max_length=255)),
                ("commit_sha", models.CharField(blank=True, max_length=64)),
                ("path", models.CharField(blank=True, max_length=700)),
                ("file_count", models.PositiveIntegerField(default=0)),
                ("total_bytes", models.PositiveBigIntegerField(default=0)),
                ("status", models.CharField(choices=[("STAGED", "Staged"), ("VERIFIED", "Verified"), ("BLOCKED", "Blocked")], default="STAGED", max_length=32)),
                ("error_code", models.CharField(blank=True, max_length=120)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("job", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="repository_snapshot", to="control.job")),
            ],
        ),
    ]
