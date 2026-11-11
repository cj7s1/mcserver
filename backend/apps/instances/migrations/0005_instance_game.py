from django.db import migrations, models

from ._other_games_guard import KeepOtherGames, noop


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
        # Last, so that undoing the migration runs it first: it refuses while servers of other games
        # exist that `upgrade.sh rollback --archive-other-games` has not recorded (_other_games_guard).
        migrations.RunPython(noop, KeepOtherGames("0005"), elidable=False),
    ]
