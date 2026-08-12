from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("control", "0013_genxcreditvaluation_cost_truth"),
    ]

    operations = [
        migrations.AlterField(
            model_name="genxcall",
            name="result_url",
            field=models.TextField(blank=True),
        ),
    ]
