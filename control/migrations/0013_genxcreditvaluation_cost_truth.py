from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("control", "0012_productcandidate_break_even_at_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="GenXCreditValuation",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version", models.CharField(max_length=80, unique=True)),
                ("currency", models.CharField(default="USD", max_length=3)),
                ("monetary_cost_per_credit", models.DecimalField(decimal_places=10, max_digits=20)),
                ("source", models.CharField(max_length=255)),
                ("evidence", models.JSONField(default=dict)),
                ("effective_at", models.DateTimeField()),
                ("verified", models.BooleanField(default=False)),
                ("active", models.BooleanField(default=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="genxcreditvaluation",
            constraint=models.CheckConstraint(
                condition=models.Q(("monetary_cost_per_credit__gt", 0)),
                name="genx_credit_valuation_cost_positive",
            ),
        ),
        migrations.AlterField(
            model_name="genxcall",
            name="cost_equivalent",
            field=models.DecimalField(blank=True, decimal_places=4, default=None, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="productcandidate",
            name="cost_basis_resolved",
            field=models.BooleanField(default=True),
        ),
    ]
