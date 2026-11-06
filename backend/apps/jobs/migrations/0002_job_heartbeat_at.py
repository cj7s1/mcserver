from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="job",
            name="heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
