# ruff: noqa: F811 (the fixtures imported from other test modules are parameters of the tests)
"""Review of the multi-game branch (2026-11-10): backups of games other than Minecraft.

Item 4: the software record of a backup kept only Minecraft's SOFTWARE_FIELDS, so restoring a
Factorio or Terraria "Before update" backup did not bring the version back.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.backups.handlers import backup_software, software_differs
from apps.backups.models import Backup
from apps.instances.handlers import SOFTWARE_FIELDS, set_software, software_state

from .test_games_phase2 import factorio  # noqa: F401
from .test_games_satisfactory import _claimed_instance
from .test_terraria import make_tr


def _restore_software(backup: Backup, inst: Any) -> dict[str, Any]:
    recorded = software_differs(backup, inst)
    assert recorded is not None, "the restore would not see that the software differs"
    return set_software(inst, recorded)


def test_factorio_backup_brings_its_version_back(factorio):
    factorio.minecraft_version = "2.0.72"
    factorio.save()
    backup = Backup(instance=factorio, name="Before update", meta={"software_before": software_state(factorio)})
    recorded = backup_software(backup)
    assert recorded == {"game": "factorio", "version": "2.0.72", "space_age": False, "image": factorio.image}
    factorio.minecraft_version = "2.1.24"
    factorio.save()
    changes = _restore_software(backup, factorio)
    factorio.refresh_from_db()
    assert changes["minecraft_version"] == ("2.1.24", "2.0.72") and factorio.minecraft_version == "2.0.72"
    assert factorio.image.endswith(":2.0.72-rootless")


@pytest.mark.parametrize("variant", ["vanilla", "tmodloader", "tshock"])
def test_terraria_backup_brings_its_version_back(make_instance, variant):
    inst = make_tr(make_instance, variant)
    before = inst.minecraft_version
    backup = Backup(instance=inst, name="Before update", meta={"software": software_state(inst)})
    recorded = backup_software(backup)
    assert recorded is not None and recorded["variant"] == variant and recorded["version"] == before
    newer = {"vanilla": "1.4.5.8", "tmodloader": "2026.10.3.0", "tshock": "1.4.5.8-6.3.0"}[variant]
    inst.minecraft_version = newer
    inst.save()
    _restore_software(backup, inst)
    inst.refresh_from_db()
    assert inst.minecraft_version == before


def test_satisfactory_backup_records_no_software_to_set_back(make_instance, fake_agent):
    inst = _claimed_instance(make_instance, fake_agent)
    backup = Backup(instance=inst, name="x", meta={"software": software_state(inst)})
    assert backup_software(backup) == {"game": "satisfactory"}
    assert software_differs(backup, inst) is None


def test_minecraft_record_is_unchanged(make_instance):
    inst = make_instance()
    state = software_state(inst)
    backup = Backup(instance=inst, name="x", meta={"software": {**state, "unknown": 1}})
    assert backup_software(backup) == {k: v for k, v in state.items() if k in SOFTWARE_FIELDS or k == "game"}
    # a record from before game definitions (no "game") is Minecraft's
    old = Backup(instance=inst, name="x", meta={"software_before": {"minecraft_version": "1.20.1", "version": "x"}})
    assert backup_software(old) == {"minecraft_version": "1.20.1"}
