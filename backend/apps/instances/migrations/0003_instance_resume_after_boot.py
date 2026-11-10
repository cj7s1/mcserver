from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("instances", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="resume_after_boot",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name="instance",
            name="resume_flagged_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
