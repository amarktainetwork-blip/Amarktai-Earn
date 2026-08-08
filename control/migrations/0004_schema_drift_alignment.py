import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("control", "0003_market_webhooks")]

    operations = [
        migrations.AlterField(
            model_name="genxcall",
            name="model",
            field=models.CharField(max_length=160),
        ),
        migrations.AlterField(
            model_name="refreshsession",
            name="jti",
            field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
        ),
    ]
