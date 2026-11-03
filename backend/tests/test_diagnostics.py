"""Diagnostics signatures (real log excerpts), crash-loop tracking and CurseForge refs."""

import pytest
from django.core.cache import cache

from apps.instances.diagnostics import analyze_logs
from apps.instances.models import Instance, ServerType
from apps.instances.reconcile import crash_restarts
from apps.instances.serializers import parse_curseforge_ref
from apps.instances.state import compute

# Excerpt from a real Better MC (Modrinth edition) crash in testing
MISSING_MODS = [
    "[18:20:33] [main/ERROR] [Missing Mods Checker/]: Error thrown while opening! Exiting",
    "java.awt.HeadlessException: ",
    "No X11 DISPLAY variable was set,",
    '2026-11-03T18:20:43.330Z\tWARN\tmc-server-runner\tMinecraft server failed. {"exitCode": 1}',
]


def make(**kw) -> Instance:
    values = {"name": "t", "game_port": 25600, "server_type": ServerType.FORGE, "minecraft_version": "1.20.1",
              "java_version": 17, "image": "itzg/minecraft-server:stable-java17", "memory_mb": 4096}
    values.update(kw)
    return Instance(**values)


def codes(lines, **kw):
    return [i.code for i in analyze_logs(make(**kw), lines)]


def test_missing_mods_checker_detected():
    issues = analyze_logs(make(), MISSING_MODS)
    assert issues[0].code == "missing_mods_checker"
    assert issues[0].actions[0]["kind"] == "manual_downloads"
    assert "Missing Mods Checker" in issues[0].evidence[0]


def test_java_class_version_maps_to_required_java():
    lines = ["java.lang.UnsupportedClassVersionError: x has been compiled by a more recent version of the Java Runtime "
             "(class file version 65.0), this version of the Java Runtime only recognizes class file versions up to 61.0"]
    issue = analyze_logs(make(java_version=17), lines)[0]
    assert issue.code == "java_too_old"
    assert issue.actions[0]["value"] == 21


def test_out_of_memory_and_oom_flag():
    assert "out_of_memory" in codes(["java.lang.OutOfMemoryError: Java heap space"])
    assert "out_of_memory" in codes([], oom_killed=True)


def test_curseforge_restricted_downloads():
    lines = ["WARN The authors of the mod 'X' have disallowed project distribution. Manually download the file"]
    assert "curseforge_manual_downloads" in codes(lines)


def test_no_false_positives_on_clean_log():
    lines = ['[Server thread/INFO]: Done (16.568s)! For help, type "help"', "[init] Starting the Minecraft server..."]
    assert codes(lines) == []


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://www.curseforge.com/minecraft/modpacks/better-mc-forge-bmc4", ("better-mc-forge-bmc4", None)),
        ("https://www.curseforge.com/minecraft/modpacks/better-mc-forge-bmc4/files/9104992", ("better-mc-forge-bmc4", "9104992")),
        ("better-mc-forge-bmc4", ("better-mc-forge-bmc4", None)),
        ("https://evil.example/minecraft/modpacks/x", None),
        ("../../etc", None),
        ("Better MC", None),
    ],
)
def test_parse_curseforge_ref(value, expected):
    assert parse_curseforge_ref(value) == expected


def test_crash_restarts_tracks_delta_from_streak_start():
    cache.clear()
    inst = make()
    inst.pk = "11111111-1111-1111-1111-111111111111"
    running = {"exists": True, "status": "running", "health": "starting"}
    assert crash_restarts(inst, {**running, "restart_count": 7}) == 0  # baseline: old restarts don't count
    assert crash_restarts(inst, {**running, "restart_count": 9}) == 2
    assert crash_restarts(inst, {**running, "restart_count": 10, "health": "healthy"}) == 0  # healthy resets
    assert crash_restarts(inst, {**running, "restart_count": 10}) == 0
    assert crash_restarts(inst, {**running, "restart_count": 0}) == 0  # recreated container resets baseline
    assert crash_restarts(inst, {**running, "restart_count": 4}) == 4


def test_compute_reports_crash_loop_instead_of_installing():
    inst = make()
    c = {"exists": True, "status": "running", "running": True, "health": "starting", "restart_count": 12}
    assert compute(inst, c)[0] == "installing"
    status, detail = compute(inst, c, crash_restarts=4)
    assert status == "failed"
    assert "Crash loop" in detail
