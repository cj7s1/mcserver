from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("instances", "0004_schedule_deferred_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="game",
            field=models.CharField(db_default="minecraft", default="minecraft", max_length=32),
        ),
    ]
