"""Contracts between the layers after the four fix packages of the 2026-11-09 audit were joined.

Each package passed its own suite against assumed contracts; these tests pin the joins: the
agent's answers (as the fake agent in conftest.py now mirrors them) and what the panel makes of
them for the frontend.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from django.core.cache import cache
from django.utils import timezone

from apps.core.models import SystemSetting
from apps.instances import reconcile
from apps.instances.models import Instance
from apps.instances.scheduling import names_fixed_hours, next_run
from apps.instances.spec import build_spec, current_spec_hash
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentClient, AgentError

from .conftest import CURRENT_AGENT_FEATURES

pytestmark = pytest.mark.django_db
REPO = Path(__file__).resolve().parents[2]
LA = ZoneInfo("America/Los_Angeles")


def err(resp):  # type: ignore[no-untyped-def]
    return resp.json()["error"]["code"]


def run(kind, inst, capture, **params) -> Job:  # type: ignore[no-untyped-def]
    with capture(execute=True):
        job = runner.enqueue(kind, title=kind, instance=inst, params=params)
    job.refresh_from_db()
    return job


@pytest.fixture
def running(make_instance, fake_agent):  # type: ignore[no-untyped-def]
    def _make(**fields):  # type: ignore[no-untyped-def]
        inst = make_instance(desired_state="running", **fields)
        fake_agent.apply(inst.id, build_spec(inst), start=True)
        fake_agent.put_file(inst.id, "server.properties", "motd=x\n")
        Instance.objects.filter(pk=inst.pk).update(
            container_spec_hash=current_spec_hash(inst),
            installed_at=timezone.now(),
            status="running",
            container_state="running",
        )
        inst.refresh_from_db()
        return inst

    return _make


# --- agent features ------------------------------------------------------------------------------------
def test_the_fake_agent_offers_what_the_agent_says_it_offers():
    source = (REPO / "agent" / "mcagent" / "main.py").read_text()
    match = re.search(r"^FEATURES = \[(.*?)\]", source, re.M | re.S)
    assert match, "FEATURES not found in agent/mcagent/main.py"
    assert tuple(re.findall(r'"([a-z_]+)"', match.group(1))) == CURRENT_AGENT_FEATURES


def test_clone_names_the_world_only_for_agents_that_know_level_name(
    make_instance, fake_agent, django_capture_on_commit_callbacks
):
    src = make_instance(name="Source")
    fake_agent.put_file(src.id, "server.properties", "level-name=survival\n")
    fake_agent.put_file(src.id, "survival/level.dat", b"w")
    fake_agent.features = ["retire"]  # an agent from before clone_level_name
    target = make_instance(name="Copy")
    job = run("clone", src, django_capture_on_commit_callbacks, target_id=str(target.id), exclude_worlds=True)
    assert job.status == JobStatus.SUCCEEDED, job.error
    _args, kwargs = fake_agent.called("clone")[-1]
    assert kwargs["level_name"] is None and kwargs["layout"] is None
    assert any("leaves out only the folders world" in line for line in job.log)


# --- text files: the agent's 415 not_utf8, encoding and *.properties ------------------------------------------
def test_a_file_the_agent_refuses_as_not_utf8_opens_as_a_read_only_preview(clients, make_instance, fake_agent):
    inst = make_instance()
    base = f"/api/instances/{inst.id}/files/text"
    fake_agent.put_file(inst.id, "plugins/x/messages.yml", b"greeting: Caf\xe9\n")
    got = clients["owner"].get(base, {"path": "plugins/x/messages.yml"})
    assert got.status_code == 200, got.content
    body = got.json()
    assert body["binary_or_non_utf8"] is True and body["encoding"] is None
    assert body["content"] == "greeting: Caf\ufffd\n" and body["size"] == len(b"greeting: Caf\xe9\n")
    # saving needs the explicit confirmation, also though the agent refuses the read
    put = clients["owner"].put(base, {"path": "plugins/x/messages.yml", "content": "greeting: Cafe\n"}, format="json")
    assert put.status_code == 409 and err(put) == "non_utf8_file"
    assert fake_agent.get_file(inst.id, "plugins/x/messages.yml") == b"greeting: Caf\xe9\n"
    ok = clients["owner"].put(
        base, {"path": "plugins/x/messages.yml", "content": "greeting: Cafe\n", "confirm_non_utf8": True}, format="json"
    )
    assert ok.status_code == 200 and fake_agent.get_file(inst.id, "plugins/x/messages.yml") == b"greeting: Cafe\n"
    # a UTF-8 file with a real U+FFFD is exact: the agent says utf-8, no byte check
    fake_agent.put_file(inst.id, "ok.txt", "real \ufffd\n".encode())
    downloads = len(fake_agent.called("download"))
    exact = clients["owner"].get(base, {"path": "ok.txt"}).json()
    assert exact["binary_or_non_utf8"] is False and exact["encoding"] == "utf-8"
    assert len(fake_agent.called("download")) == downloads


def test_a_latin1_properties_file_round_trips_through_the_editor_without_double_encoding(clients, make_instance, fake_agent):
    inst = make_instance()
    base = f"/api/instances/{inst.id}/files/text"
    fake_agent.put_file(inst.id, "plugins/x/lang.properties", b"title=\xa76Caf\xe9\n")
    got = clients["owner"].get(base, {"path": "plugins/x/lang.properties"}).json()
    assert got == {**got, "content": "title=§6Café\n", "encoding": "iso-8859-1", "binary_or_non_utf8": False}
    # unchanged text goes back as the same bytes; what ISO-8859-1 cannot store becomes \uXXXX
    put = clients["owner"].put(base, {"path": "plugins/x/lang.properties", "content": "title=§6Café €\n"}, format="json")
    assert put.status_code == 200, put.content
    assert fake_agent.get_file(inst.id, "plugins/x/lang.properties") == b"title=\xa76Caf\xe9 \\u20ac\n"
    assert fake_agent.called("write_text")[-1][1]["encoding"] == "iso-8859-1"
    again = clients["owner"].get(base, {"path": "plugins/x/lang.properties"}).json()
    assert again["content"] == "title=§6Café \\u20ac\n"  # read back as written: no second escaping
    clients["owner"].put(base, {"path": "plugins/x/lang.properties", "content": again["content"]}, format="json")
    assert fake_agent.get_file(inst.id, "plugins/x/lang.properties") == b"title=\xa76Caf\xe9 \\u20ac\n"
    # a UTF-8 .properties file stays UTF-8, written as typed
    fake_agent.put_file(inst.id, "u.properties", "a=é\n".encode())
    clients["owner"].put(base, {"path": "u.properties", "content": "a=é €\n"}, format="json")
    assert fake_agent.get_file(inst.id, "u.properties") == "a=é €\n".encode()


def test_server_properties_from_the_strict_agent_keeps_latin1_and_escapes(clients, make_instance, fake_agent):
    inst = make_instance()
    fake_agent.put_file(inst.id, "server.properties", b"motd=\xa76Hi\nwhite-list=false\n")
    shown = clients["owner"].get(f"/api/instances/{inst.id}/properties").json()
    assert shown["values"]["motd"] == "§6Hi"
    assert not fake_agent.called("download")  # the agent decoded it as ISO-8859-1 itself
    clients["owner"].patch(f"/api/instances/{inst.id}/properties", {"values": {"white-list": True}}, format="json")
    data = fake_agent.get_file(inst.id, "server.properties")
    assert data.isascii() and b"white-list=true" in data


def test_an_older_lossy_agent_is_still_detected_by_the_bytes(clients, make_instance, fake_agent):
    fake_agent.strict_text = False
    inst = make_instance()
    fake_agent.put_file(inst.id, "c.yml", b"x: \xe9\n")
    body = clients["owner"].get(f"/api/instances/{inst.id}/files/text", {"path": "c.yml"}).json()
    assert body["binary_or_non_utf8"] is True and "encoding" not in body


def test_a_non_utf8_bukkit_yml_does_not_break_the_world_reset_plan(make_instance, fake_agent):
    from apps.instances import world

    inst = make_instance(server_type="PAPER")
    fake_agent.put_file(inst.id, "bukkit.yml", b"settings:\n  world-container: welt\xe9n\n")
    blockers = world._world_location_blockers(inst, AgentClient(inst.node), "bukkit")
    assert [b["code"] for b in blockers] == ["world_location_overridden"]


# --- hashes: too_large, hashing_busy, suffix ----------------------------------------------------------------
@pytest.fixture
def mod_server(make_instance):  # type: ignore[no-untyped-def]
    return make_instance(server_type="FABRIC", minecraft_version="1.20.4")


def test_a_jar_too_large_to_hash_is_unidentified_and_never_pending(mod_server, fake_agent):
    from apps.content import service

    fake_agent.put_file(mod_server.id, "mods/huge.jar", b"x")
    fake_agent.put_file(mod_server.id, "mods/notes.txt", b"x")
    fake_agent.too_large_files = {"mods/huge.jar"}
    for wait in (None, 3.0):
        inv = service.inventory(mod_server, wait=wait)
        assert [(f["filename"], f["source"], f["sha1"]) for f in inv["files"]] == [("huge.jar", "too_large", None)]
        assert inv["identification"]["complete"] is True and inv["identification"]["pending"] == 0
    assert fake_agent.called("hash_files")[-1][1]["suffixes"] == (".jar", ".jar.disabled")


def test_an_agent_busy_hashing_shows_the_jars_as_pending(mod_server, fake_agent):
    from apps.content import service

    fake_agent.put_file(mod_server.id, "mods/a.jar", b"a")
    fake_agent.put_file(mod_server.id, "mods/b.jar.disabled", b"b")
    fake_agent.failures["hash_files"] = AgentError("Reading the files takes longer", "hashing_busy", 503)
    inv = service.inventory(mod_server)
    assert [(f["filename"], f["source"], f["enabled"]) for f in inv["files"]] == [
        ("a.jar", "pending", True),
        ("b.jar.disabled", "pending", False),
    ]
    assert inv["identification"]["pending"] == 2
    # other agent errors are still errors
    fake_agent.failures["hash_files"] = AgentError("boom", "agent_error", 500)
    with pytest.raises(AgentError):
        service.inventory(mod_server)


@respx.mock
def test_client_sends_suffixes_and_does_not_wait_for_too_large_files(node):
    import uuid

    iid = uuid.uuid4()
    route = respx.get(f"{node.agent_url}/v1/instances/{iid}/files/hashes").mock(
        return_value=httpx.Response(
            200, json={"files": [{"name": "big.jar", "hash": None, "too_large": True}, {"name": "a.jar", "hash": "ab" * 20}]}
        )
    )
    out = AgentClient(node).hash_files_partial(iid, "mods", wait=2.0, suffixes=(".jar", ".jar.disabled"))
    assert out["complete"] is True and out["pending"] == 0
    assert route.calls[0].request.url.params.get_list("suffix") == [".jar", ".jar.disabled"]


# --- system settings: memory reserve, bind address ------------------------------------------------------------
def test_memory_reserve_is_an_editable_setting_with_limits(clients):
    url = "/api/system/settings"
    assert clients["owner"].get(url).json()["values"]["memory_reserve_mb"] == 4096
    ok = clients["owner"].patch(url, {"values": {"memory_reserve_mb": 2048}}, format="json")
    assert ok.status_code == 200 and ok.json()["values"]["memory_reserve_mb"] == 2048 and ok.json()["warnings"] == []
    from apps.instances import capacity

    assert capacity.reserve_mb() == 2048
    assert clients["owner"].patch(url, {"values": {"memory_reserve_mb": 0}}, format="json").status_code == 200
    for bad in (-1, 65537, True, 1.5, "4096", None):
        resp = clients["owner"].patch(url, {"values": {"memory_reserve_mb": bad, "timezone": "UTC"}}, format="json")
        assert resp.status_code == 400 and err(resp) == "invalid_setting", bad
    # nothing of a refused request is saved
    assert SystemSetting.get("memory_reserve_mb") == 0 and SystemSetting.get("timezone") == ""


def test_a_bind_address_that_is_no_ip_is_refused(clients):
    url = "/api/system/settings"
    resp = clients["owner"].patch(url, {"values": {"default_bind_ip": "192.168.1.5:8484"}}, format="json")
    assert resp.status_code == 400 and err(resp) == "invalid_setting"
    assert clients["owner"].patch(url, {"values": {"default_bind_ip": "::"}}, format="json").status_code == 200


# --- modpack pinning from the reconciler ---------------------------------------------------------------------
def test_the_reconciler_retries_pinning_a_keyless_pack(make_instance, fake_agent):
    inst = make_instance(
        server_type="AUTO_CURSEFORGE",
        modpack_provider="curseforge",
        modpack_slug="bmc",
        desired_state="running",
        installed_at=timezone.now(),
        status="running",
    )
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    reconcile.apply_observation(inst, fake_agent.status(inst.id))  # no manifest yet: nothing recorded
    inst.refresh_from_db()
    assert inst.modpack_project_id in ("", None)
    fake_agent.put_file(
        inst.id,
        ".curseforge-manifest.json",
        json.dumps({"fileId": 5001, "modId": 77, "name": "BMC", "modLoaderId": "fabric-0.15", "minecraftVersion": "1.20.1"}),
    )
    reconcile.apply_observation(inst, fake_agent.status(inst.id))
    inst.refresh_from_db()
    assert inst.modpack_project_id in ("", None)  # rate-limited: at most once a minute
    cache.clear()
    reconcile.apply_observation(inst, fake_agent.status(inst.id))
    inst.refresh_from_db()
    assert (inst.modpack_project_id, inst.modpack_version_id, inst.modpack_loader) == ("77", "5001", "fabric")


# --- import archive refused before the operation started ---------------------------------------------------------
def test_a_refused_import_deletes_its_uploaded_archive(make_instance, fake_agent, django_capture_on_commit_callbacks):
    inst = make_instance()
    fake_agent.apply(inst.id, build_spec(inst), start=True)  # running on the node, whatever the panel thinks
    fake_agent.put_file(inst.id, ".mcpanel-import-0123456789ab.zip", b"PK")
    fake_agent.put_file(inst.id, "mine.zip", b"PK")
    job = run(
        "extract_archive",
        inst,
        django_capture_on_commit_callbacks,
        path=".mcpanel-import-0123456789ab.zip",
        destination="",
        strip="auto",
        overwrite=False,
        delete_archive=True,
        mark_installed=True,
    )
    assert job.status == JobStatus.FAILED and "stopped" in job.error
    assert fake_agent.get_file(inst.id, ".mcpanel-import-0123456789ab.zip") is None
    # an archive the user put there is never deleted without being extracted
    job = run("extract_archive", inst, django_capture_on_commit_callbacks, path="mine.zip", delete_archive=True)
    assert job.status == JobStatus.FAILED and fake_agent.get_file(inst.id, "mine.zip") == b"PK"


# --- apply refused with cpu_limit / create_failed: nothing changed ------------------------------------------------
@pytest.mark.parametrize("code", ["cpu_limit", "create_failed"])
def test_a_refused_apply_starts_the_server_again(running, fake_agent, django_capture_on_commit_callbacks, code):
    inst = running()
    fake_agent.failed_operations["apply"] = ("Docker refused", code)
    job = run("recreate", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.FAILED
    assert any("Nothing was changed; the server was started again" in line for line in job.log), job.log
    assert fake_agent.containers[str(inst.id)]["running"]


def test_a_failed_rename_after_the_removal_is_not_nothing_changed(running, fake_agent, django_capture_on_commit_callbacks):
    inst = running()
    fake_agent.failed_operations["apply"] = ("Docker could not rename the new container", "rename_failed")
    job = run("recreate", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.FAILED
    inst.refresh_from_db()
    assert inst.desired_state == "stopped"


def test_a_cpu_limit_above_the_node_cpus_is_refused_when_saved(clients, make_instance, fake_agent):
    inst = make_instance()
    resp = clients["owner"].patch(f"/api/instances/{inst.id}", {"cpu_limit": 6}, format="json")  # the fake node has 4
    assert resp.status_code == 400 and "4 CPUs" in json.dumps(resp.json())
    assert clients["owner"].patch(f"/api/instances/{inst.id}", {"cpu_limit": 2}, format="json").status_code == 200


# --- players for polling pages: ping, not RCON ----------------------------------------------------------------
def test_the_stats_endpoint_counts_players_by_ping(clients, running, fake_agent):
    inst = running()
    fake_agent.ping_answer = {"online": 2, "max": 10, "sample": [{"name": "Alex"}, {"name": "Steve"}]}
    for _ in range(3):
        body = clients["owner"].get(f"/api/instances/{inst.id}/stats").json()
    assert body["players"] == {"online": 2, "max": 10, "players": ["Alex", "Steve"]}
    assert fake_agent.commands == []  # no RCON connection, nothing in the server's log
    assert len(fake_agent.called("ping")) == 1  # cached between the polls
    cache.clear()
    fake_agent.failures["ping"] = AgentError("still starting", "ping_failed", 502)
    assert clients["owner"].get(f"/api/instances/{inst.id}/stats").json()["players"] is None
    assert fake_agent.commands == []


# --- daylight saving: fixed hours run once, intervals on real time ------------------------------------------------
def _utc_seq(expr: str, start: datetime, n: int) -> list[str]:
    out, t = [], start
    for _ in range(n):
        t = next_run(expr, t, LA)
        out.append(t.astimezone(UTC).strftime("%H:%M"))
    return out


def test_interval_schedules_run_through_the_repeated_hour():
    # 2027-11-07 in Los Angeles: 01:00-01:59 PDT (08:xx UTC) and again PST (09:xx UTC)
    assert _utc_seq("0 * * * *", datetime(2027, 11, 7, 7, 30, tzinfo=UTC), 4) == ["08:00", "09:00", "10:00", "11:00"]
    assert _utc_seq("*/30 * * * *", datetime(2027, 11, 7, 7, 50, tzinfo=UTC), 5) == ["08:00", "08:30", "09:00", "09:30", "10:00"]
    assert _utc_seq("15 */1 * * *", datetime(2027, 11, 7, 8, 20, tzinfo=UTC), 2) == ["09:15", "10:15"]
    # a tick inside the second occurrence goes on from there
    assert next_run("0 * * * *", datetime(2027, 11, 7, 9, 0, 30, tzinfo=UTC), LA).astimezone(UTC).hour == 10


def test_fixed_hour_schedules_run_once_in_the_repeated_hour():
    assert _utc_seq("30 1 * * *", datetime(2027, 11, 7, 7, 0, tzinfo=UTC), 2) == ["08:30", "09:30"]
    second = next_run("30 1 * * *", datetime(2027, 11, 7, 8, 31, tzinfo=UTC), LA)
    assert second.astimezone(LA).day == 8  # not again at 01:30 PST
    assert _utc_seq("0 1,2 * * *", datetime(2027, 11, 7, 7, 0, tzinfo=UTC), 2) == ["08:00", "10:00"]


def test_interval_schedules_keep_real_time_over_the_skipped_hour():
    # 2027-03-14: 02:00 PST jumps to 03:00 PDT (10:00 UTC)
    assert _utc_seq("0 * * * *", datetime(2027, 3, 14, 8, 30, tzinfo=UTC), 3) == ["09:00", "10:00", "11:00"]


@pytest.mark.parametrize(
    ("expr", "fixed"),
    [
        ("30 2 * * *", True),
        ("0 1,13 * * *", True),
        ("0 1-3 * * *", True),
        ("0 * * * *", False),
        ("0 */2 * * *", False),
        ("0 1-23/2 * * *", False),
    ],
)
def test_which_schedules_name_fixed_hours(expr, fixed):
    assert names_fixed_hours(expr) is fixed


def test_next_run_is_unchanged_far_from_daylight_saving():
    after = datetime(2027, 6, 1, 0, 0, tzinfo=UTC)
    assert next_run("0 4 * * *", after, LA) == datetime(2027, 6, 1, 4, 0, tzinfo=LA)
    assert next_run("*/5 * * * *", after, LA) == after + timedelta(minutes=5)


def test_ping_sample_entries_that_are_no_player_names_are_dropped(clients, running, fake_agent):
    inst = running()
    fake_agent.ping_answer = {"online": 40, "max": 50, "sample": [{"name": "§aWelcome to the server"}, {"name": "Alex"}, "Alex"]}
    assert clients["owner"].get(f"/api/instances/{inst.id}/stats").json()["players"]["players"] == ["Alex"]
