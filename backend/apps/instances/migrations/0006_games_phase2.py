"""Multi-game phase 2: named ports, pinned game options, per-instance secrets and setup state.

Every new column has a database default, so a rollback to the release before keeps inserting rows.
The (node, game_port) uniqueness now holds for Minecraft servers only: other games check their named
ports per protocol in code (UDP 34197 and TCP 34197 are different ports).
"""

from django.db import migrations, models

import apps.core.crypto


class Migration(migrations.Migration):
    dependencies = [
        ("instances", "0005_instance_game"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="instance",
            name="unique_game_port_per_node",
        ),
        migrations.AddField(
            model_name="instance",
            name="game_options",
            field=models.JSONField(blank=True, db_default={}, default=dict),
        ),
        migrations.AddField(
            model_name="instance",
            name="ports",
            field=models.JSONField(blank=True, db_default=[], default=list),
        ),
        migrations.AddField(
            model_name="instance",
            name="secrets",
            field=apps.core.crypto.EncryptedTextField(blank=True, db_default="", default=""),
        ),
        migrations.AddField(
            model_name="instance",
            name="setup_state",
            field=models.JSONField(blank=True, db_default={}, default=dict),
        ),
        migrations.AddConstraint(
            model_name="instance",
            constraint=models.UniqueConstraint(
                condition=models.Q(("game", "minecraft")), fields=("node", "game_port"), name="unique_game_port_per_node"
            ),
        ),
    ]
