from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("instances", "0003_instance_resume_after_boot"),
    ]

    operations = [
        migrations.AddField(
            model_name="schedule",
            name="deferred_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
