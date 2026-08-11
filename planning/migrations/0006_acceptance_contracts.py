from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("control", "0013_genxcreditvaluation_cost_truth"),
        ("planning", "0005_workplan_escalation_policy_workplan_max_repair_cost_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="AcceptanceContract",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("version", models.PositiveIntegerField()),
                ("status", models.CharField(choices=[("ACTIVE", "Active"), ("STALE", "Stale"), ("BLOCKED", "Blocked")], default="ACTIVE", max_length=20)),
                ("is_current", models.BooleanField(default=True)),
                ("source_hash", models.CharField(max_length=64)),
                ("compiler_version", models.CharField(default="acceptance-compiler-v1", max_length=40)),
                ("source_requirements", models.JSONField(default=dict)),
                ("compiled_task", models.JSONField(default=dict)),
                ("criteria", models.JSONField(default=list)),
                ("reason_codes", models.JSONField(default=list)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="acceptance_contracts", to="control.job")),
            ],
            options={"ordering": ["-version", "-created_at"]},
        ),
        migrations.CreateModel(
            name="AcceptanceEvaluation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("evaluator_version", models.CharField(default="acceptance-evaluator-v1", max_length=40)),
                ("deterministic_passed", models.BooleanField(default=False)),
                ("semantic_state", models.CharField(choices=[("PASS", "Pass"), ("FAIL", "Fail"), ("UNCERTAIN", "Uncertain")], default="UNCERTAIN", max_length=16)),
                ("submission_ready", models.BooleanField(default=False)),
                ("criterion_results", models.JSONField(default=list)),
                ("critical_failures", models.JSONField(default=list)),
                ("evidence", models.JSONField(default=dict)),
                ("contract", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="evaluations", to="planning.acceptancecontract")),
                ("execution", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="acceptance_evaluation", to="control.execution")),
            ],
        ),
        migrations.AddConstraint(
            model_name="acceptancecontract",
            constraint=models.UniqueConstraint(fields=("job", "version"), name="uniq_acceptance_contract_job_version"),
        ),
        migrations.AddConstraint(
            model_name="acceptancecontract",
            constraint=models.UniqueConstraint(condition=models.Q(("is_current", True)), fields=("job",), name="uniq_current_acceptance_contract_per_job"),
        ),
    ]
