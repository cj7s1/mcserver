"""Fixes of the security and bug review of 2026-11-11, slice C (audit-2026-11-09/security-review/c-untrusted-data).

The reviewers' proofs (``c-untrusted-data/poc/backend/test_c_backend.py``) passed while each weakness was present;
the tests here assert the fixed behaviour.

* C-8: TShock's one-time ``/setup`` code is switched off (``setup.lock``) and its file is a secret file.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps import games
from apps.games import terraria
from apps.instances import secret_files
from apps.nodes.client import AgentClient

from .test_terraria import make_tr


# --- C-8 ------------------------------------------------------------------------------------------------------
def test_c8_a_new_tshock_server_has_the_setup_lock(make_instance: Any) -> None:
    inst = make_tr(make_instance, "tshock")
    files = games.for_instance(inst).initial_files(inst, {})
    assert files[terraria.SETUP_LOCK] == ""
    vanilla = make_tr(make_instance, "vanilla", game_port=7778)
    assert terraria.SETUP_LOCK not in games.for_instance(vanilla).initial_files(vanilla, {})


def test_c8_every_apply_writes_the_lock_and_deletes_a_setup_code(make_instance: Any, fake_agent: Any) -> None:
    """A server from before the fix (or a restored / imported folder): no lock, TShock's code file in place."""
    inst = make_tr(make_instance, "tshock")
    fake_agent.put_file(inst.id, "setup-code.txt", "4815162")
    games.for_instance(inst).before_apply(inst, AgentClient(inst.node))
    assert fake_agent.text(inst.id, "setup.lock") == ""
    assert fake_agent.get_file(inst.id, "setup-code.txt") is None
    # again: nothing to do (an existing lock is kept as it is)
    fake_agent.put_file(inst.id, "setup.lock", "kept")
    games.for_instance(inst).before_apply(inst, AgentClient(inst.node))
    assert fake_agent.text(inst.id, "setup.lock") == "kept"


def test_c8_vanilla_gets_no_lock(make_instance: Any, fake_agent: Any) -> None:
    inst = make_tr(make_instance, "vanilla")
    fake_agent.prepare(inst.id)
    games.for_instance(inst).before_apply(inst, AgentClient(inst.node))
    assert fake_agent.get_file(inst.id, "setup.lock") is None


def test_c8_the_setup_code_file_is_a_secret_file(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    """The PoC's operator read ``setup-code.txt`` in clear through the file manager."""
    inst = make_tr(make_instance, "tshock")
    fake_agent.put_file(inst.id, "setup-code.txt", "4815162")
    assert secret_files.is_secret(inst, "setup-code.txt")
    base = f"/api/instances/{inst.id}/files"
    for client in (clients["operator"], clients["owner"]):
        shown = client.get(f"{base}/text", {"path": "setup-code.txt"}).json()
        assert shown["redacted"] is True and "4815162" not in shown["content"]
        assert client.get(f"{base}/download", {"path": "setup-code.txt"}).status_code == 403
    # only TShock has one
    assert not secret_files.is_secret(make_tr(make_instance, "vanilla", game_port=7779), "setup-code.txt")


# --- C-1 ------------------------------------------------------------------------------------------------------
# what a Terraria player can make the server print: chat is "<name> text" in the console log
CHAT_FAKE_PLAYER = "<mallory> Admin (10.0.0.1)"
CHAT_FAKE_COUNT = "<mallory> 99999999999 players connected"
REAL_PLAYING = ": Ann (172.18.0.1:5000)\n1 player connected."


def test_c1_terraria_chat_lines_are_neither_players_nor_counts() -> None:
    parsed = terraria.parse_playing(f"{REAL_PLAYING}\n{CHAT_FAKE_PLAYER}\n{CHAT_FAKE_COUNT}\n: <bob> 7 players connected.")
    assert parsed == {"online": 1, "max": None, "players": ["Ann"]}
    # the game's own lines still parse
    assert terraria.parse_playing(": Ann (172.18.0.1:52344)\n: Bob Two (10.0.0.5:1)\n2 players connected.\n")["online"] == 2
    assert terraria.parse_playing(": No players connected.")["online"] == 0
    # an unchecked number is never taken as the count (and nothing else was recognised: unknown, review F-2)
    assert terraria.parse_playing("1234 players connected.")["online"] is None


def test_c1_bounded_count() -> None:
    from apps.metrics.tasks import MAX_PLAYER_COUNT, bounded_count

    assert [bounded_count(v) for v in (0, 5, 5.0, "12", MAX_PLAYER_COUNT)] == [0, 5, 5, 12, MAX_PLAYER_COUNT]
    for bad in (-1, MAX_PLAYER_COUNT + 1, 10**12, 2.5, "x", "99999999999", None, True, [], {}):
        assert bounded_count(bad) is None, bad


@pytest.mark.django_db(transaction=True)
def test_c1_one_chat_line_no_longer_stops_the_metrics_of_other_servers(make_instance: Any, fake_agent: Any) -> None:
    from django.core.cache import cache

    from apps.instances.models import Instance
    from apps.instances.spec import build_spec
    from apps.metrics.models import MetricSample
    from apps.metrics.tasks import sample_all

    from .test_terraria import start_container

    first = make_instance(name="A minecraft")
    terra = make_tr(make_instance, name="B public terraria")
    last = make_instance(name="C minecraft")
    for inst in (first, last):
        fake_agent.apply(inst.id, build_spec(inst), start=True)
    start_container(fake_agent, terra)
    Instance.objects.update(status="running")
    # a count that is out of range for the database even after the parser (a compromised server's own answer)
    real = games.get("terraria").online_players
    games.get("terraria").online_players = lambda *a, **k: {"online": 10**11, "max": 10**11, "players": ["x" * 300]}
    try:
        cache.clear()
        assert sample_all() == 3
    finally:
        games.get("terraria").online_players = real
    sample = MetricSample.objects.get(instance=terra)
    assert sample.players_online is None and sample.players_max is None
    assert MetricSample.objects.filter(instance=first).count() == 1 and MetricSample.objects.filter(instance=last).count() == 1
    from apps.metrics.models import PlayerSeen

    assert [len(p.name) for p in PlayerSeen.objects.filter(instance=terra)] == [64]


@pytest.mark.django_db(transaction=True)
def test_c1_a_lying_status_ping_is_dropped(make_instance: Any, fake_agent: Any) -> None:
    from apps.instances.models import Instance
    from apps.instances.spec import build_spec
    from apps.metrics.models import MetricSample
    from apps.metrics.tasks import sample_all

    liar = make_instance(name="A compromised")
    other = make_instance(name="B healthy")
    for inst in (liar, other):
        fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.update(status="running")
    fake_agent.ping_answer = {"online": 10**12, "max": 20, "sample": []}
    assert sample_all() == 2
    assert MetricSample.objects.get(instance=liar).players_online is None
    assert MetricSample.objects.get(instance=other).players_max == 20


@pytest.mark.django_db(transaction=True)
def test_c1_any_error_of_one_server_is_contained(make_instance: Any, fake_agent: Any, monkeypatch: Any) -> None:
    from apps.instances.models import Instance
    from apps.instances.spec import build_spec
    from apps.metrics import tasks
    from apps.metrics.models import MetricSample

    a = make_instance(name="A")
    b = make_instance(name="B")
    for inst in (a, b):
        fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.update(status="running")
    real = tasks.player_count

    def player_count(inst: Any, client: Any) -> Any:
        if inst.pk == a.pk:
            raise RuntimeError("boom")
        return real(inst, client)

    monkeypatch.setattr(tasks, "player_count", player_count)
    assert tasks.sample_all() == 1
    assert MetricSample.objects.filter(instance=b).count() == 1


def test_c1_player_history_names_and_clean_up(make_instance: Any) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from apps.metrics.models import PlayerSeen
    from apps.metrics.tasks import cleanup, record_players

    inst = make_instance()
    now = timezone.now()
    long_names = ["SomeoneWithALongName-1", "SomeoneWithALongName-2"]  # one player each, not merged at 16 characters
    record_players(inst, long_names, now)
    record_players(inst, long_names, now)  # again: counted, no unique-constraint error
    assert sorted(PlayerSeen.objects.filter(instance=inst).values_list("name", flat=True)) == long_names
    assert {p.minutes_online for p in PlayerSeen.objects.filter(instance=inst)} == {2}
    PlayerSeen.objects.filter(name=long_names[0]).update(last_seen=now - timedelta(days=91))
    cleanup()
    assert list(PlayerSeen.objects.filter(instance=inst).values_list("name", flat=True)) == [long_names[1]]


# --- C-5 (I-4) and C-9 ----------------------------------------------------------------------------------------
@pytest.fixture
def six_actions(fake_agent: Any, monkeypatch: Any) -> Any:
    from .test_games_six import FakeActions

    fake = FakeActions(fake_agent)
    monkeypatch.setattr(AgentClient, "game_action", lambda client, *a, **k: fake(client, *a, **k))
    return fake


def _post_player(clients: Any, inst: Any, **body: Any) -> Any:
    return clients["owner"].post(f"/api/instances/{inst.id}/players", body, format="json")


def test_c5_rust_owner_by_a_name_two_players_share_is_refused_with_the_candidates(
    clients: Any, fake_agent: Any, six_actions: Any, django_capture_on_commit_callbacks: Any
) -> None:
    """The PoC: an impostor named like the admin's friend joined first and got ``ownerid``."""
    from .test_games_six import create
    from .test_games_six import start as start_six

    inst = create(clients, django_capture_on_commit_callbacks, "rust")
    start_six(fake_agent, inst)
    six_actions.players = [
        {"SteamID": "76561190000000666", "DisplayName": "Colton", "Ping": 40},
        {"SteamID": "76561190000000001", "DisplayName": "colton", "Ping": 12},
    ]
    for action in ("op", "kick", "ban"):
        resp = _post_player(clients, inst, action=action, name="colton")
        assert resp.status_code == 409, resp.json()
        error = resp.json()["error"]
        assert error["code"] == "ambiguous_player"
        assert error["details"]["candidates"] == [
            {"name": "Colton", "id": "76561190000000666"},
            {"name": "colton", "id": "76561190000000001"},
        ]
    assert not [c for c in six_actions.calls if c[0] != "players"]  # nothing was run
    # one match: a kick goes to that player's id; owner and ban want the id
    six_actions.players = six_actions.players[1:]
    assert _post_player(clients, inst, action="kick", name="colton").status_code == 200
    assert six_actions.calls[-1] == ("kick", {"steamid": "76561190000000001", "reason": ""})
    assert _post_player(clients, inst, action="ban", name="colton").json()["error"]["code"] == "ambiguous_player"
    assert _post_player(clients, inst, action="op", name="76561190000000001").status_code == 200
    assert six_actions.calls[-1] == ("owner_add", {"steamid": "76561190000000001", "reason": "", "name": "colton"})


def test_c5_palworld_ban_by_name_needs_the_id(
    clients: Any, fake_agent: Any, six_actions: Any, django_capture_on_commit_callbacks: Any
) -> None:
    from .test_games_six import create
    from .test_games_six import start as start_six

    inst = create(clients, django_capture_on_commit_callbacks, "palworld")
    start_six(fake_agent, inst)
    six_actions.players = [
        {"name": "Kiki", "userId": "steam_76561190000000009"},
        {"name": "kiki", "userId": "steam_76561190000000010"},
    ]
    resp = _post_player(clients, inst, action="kick", name="Kiki")
    assert resp.status_code == 409 and len(resp.json()["error"]["details"]["candidates"]) == 2
    six_actions.players = six_actions.players[:1]
    resp = _post_player(clients, inst, action="ban", name="Kiki")
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["candidates"] == [{"name": "Kiki", "id": "steam_76561190000000009"}]
    assert _post_player(clients, inst, action="kick", name="Kiki").status_code == 200
    assert six_actions.calls[-1] == ("kick", {"userid": "steam_76561190000000009"})
    assert _post_player(clients, inst, action="ban", name="steam_76561190000000009", reason="grief").status_code == 200
    assert six_actions.calls[-1] == ("ban", {"userid": "steam_76561190000000009", "message": "grief"})


def test_c5_c9_tshock_names_the_player_exactly_and_quotes_the_ban_reason(
    clients: Any, make_instance: Any, fake_agent: Any
) -> None:
    from .test_terraria import start_container

    inst = make_tr(make_instance, "tshock")
    start_container(fake_agent, inst)
    url = f"/api/instances/{inst.id}/players"
    owner = clients["owner"]
    assert owner.post(url, {"action": "kick", "name": "Bo", "reason": "go home"}, format="json").status_code == 200
    assert fake_agent.commands[-1] == (str(inst.id), 'kick "tsn:Bo" go home')
    assert owner.post(url, {"action": "ban", "name": "3", "reason": "griefing spawn 1d"}, format="json").status_code == 200
    assert fake_agent.commands[-1] == (str(inst.id), 'ban add "tsn:3" "griefing spawn 1d"')
    owner.post(url, {"action": "ban", "name": "Bob", "reason": 'say "hi"'}, format="json")
    assert fake_agent.commands[-1] == (str(inst.id), 'ban add "tsn:Bob" "say \'hi\'"')
    owner.post(url, {"action": "ban", "name": "Bob", "reason": "-a"}, format="json")
    assert fake_agent.commands[-1] == (str(inst.id), 'ban add "tsn:Bob" "-a."')
    owner.post(url, {"action": "ban", "name": "Bob"}, format="json")
    assert fake_agent.commands[-1] == (str(inst.id), 'ban add "tsn:Bob"')


def test_c5_a_minecraft_nickname_is_never_taken_for_an_account() -> None:
    from apps.players.service import parse_list

    out = "There are 3 out of maximum 20 players online.\ndefault: ~Notch, Alice, [AFK]~Bob, .BedrockSteve"
    parsed = parse_list(out)
    assert parsed["players"] == ["Alice"]
    assert parsed["other_names"] == ["~Notch", "~Bob", ".BedrockSteve"]
    assert "other_names" not in parse_list("There are 1 of a max of 20 players online: Alice")


# --- C-7 ------------------------------------------------------------------------------------------------------
def test_c7_chat_lines_make_no_diagnosis(make_instance: Any) -> None:
    from apps.instances.diagnostics import analyze_logs

    inst = make_instance(java_version=21)
    chat = [
        '[20:15:03] [Server thread/INFO]: Done (12.3s)! For help, type "help"',
        "[20:16:10] [Server thread/INFO]: <mallory> class file version 52.0, this runtime only recognizes up to 51.0",
        "[20:16:12] [Server thread/INFO]: <mallory> java.lang.OutOfMemoryError: Java heap space",
        "[20:16:13] [Async Chat Thread - #0/INFO]: [Not Secure] <mallory> FAILED TO BIND TO PORT",
        "[20:16:14 INFO]: * mallory Could not reserve enough space for object heap",
    ]
    assert analyze_logs(inst, chat) == []
    # the server's own lines still are diagnosed
    real = ["[20:16:12] [Server thread/ERROR]: java.lang.OutOfMemoryError: Java heap space"]
    assert {i.code for i in analyze_logs(inst, chat + real)} == {"out_of_memory"}


def test_c7_steam_games_chat_lines_make_no_diagnosis(
    clients: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any
) -> None:
    from .test_games_six import create
    from .test_games_six import start as start_six

    inst = create(clients, django_capture_on_commit_callbacks, "rust")
    start_six(fake_agent, inst)
    fake_agent.log_lines[str(inst.id)] = ["[CHAT] mallory : No space left on device", "<mallory> Out of memory"]
    assert clients["owner"].get(f"/api/instances/{inst.id}/diagnostics").json()["issues"] == []
    fake_agent.log_lines[str(inst.id)] = ["write failed: No space left on device"]
    issues = {i["code"] for i in clients["owner"].get(f"/api/instances/{inst.id}/diagnostics").json()["issues"]}
    assert "disk_full" in issues


# --- C-10 -----------------------------------------------------------------------------------------------------
def test_c10_catalog_requests_stay_with_the_upstream_hosts() -> None:
    import respx

    from apps.catalog import providers
    from apps.core.errors import UpstreamError

    with respx.mock(assert_all_called=False) as mock:
        internal = mock.get("http://10.0.0.5/secret").respond(200, json={"x": 1})
        mock.get("https://meta.fabricmc.net/v2/versions/game").respond(302, headers={"Location": "http://10.0.0.5/secret"})
        mock.get("https://meta.quiltmc.org/v3/versions/game").respond(302, headers={"Location": "/v3/moved"})
        mock.get("https://meta.quiltmc.org/v3/moved").respond(200, json=[{"version": "1.21"}])
        mock.get("https://fill.papermc.io/v3/projects/paper/versions").respond(
            200, content=b"[" + b"1," * (20 * 1024 * 1024) + b"1]"
        )
        with pytest.raises(UpstreamError):
            providers._get("https://meta.fabricmc.net/v2/versions/game")  # redirected into the internal network
        with pytest.raises(UpstreamError):
            providers._get("http://10.0.0.5/secret")  # a URL from an answer (Mojang's manifest, Docker Hub's "next")
        assert not internal.called
        assert providers._get("https://meta.quiltmc.org/v3/versions/game") == [{"version": "1.21"}]  # same host: fine
        with pytest.raises(UpstreamError):
            providers._get("https://fill.papermc.io/v3/projects/paper/versions")  # 40 MB


def test_c10_provider_requests_keep_the_api_key_on_its_host() -> None:
    import respx

    from apps.core.errors import UpstreamError
    from apps.modpacks.providers import _request

    with respx.mock(assert_all_called=False) as mock:
        elsewhere = mock.get("https://collector.example/v1/mods").respond(200, json={})
        mock.get("https://api.curseforge.com/v1/mods").respond(301, headers={"Location": "https://collector.example/v1/mods"})
        with pytest.raises(UpstreamError):
            _request("GET", "https://api.curseforge.com/v1/mods", headers={"x-api-key": "k"}, provider="CurseForge")
        assert not elsewhere.called
        mock.get("https://api.modrinth.com/v2/project/x").respond(200, json={"id": "x"})
        assert _request("GET", "https://api.modrinth.com/v2/project/x", provider="Modrinth") == {"id": "x"}


# --- slice C suspicion: a malformed .mrpack index -------------------------------------------------------------
@pytest.mark.parametrize(
    "index",
    [
        [1, 2, 3],  # a list instead of an object
        {"files": ["mods/a.jar", 5], "dependencies": ["minecraft"]},  # entries that are no objects
        {"files": [{"path": "mods/a.jar", "env": "server"}], "dependencies": {"minecraft": "1.20.1"}},
    ],
)
def test_a_malformed_pack_index_is_not_a_500(index: Any, settings: Any) -> None:
    import io
    import json
    import zipfile

    import respx

    from apps.modpacks.mrpack import inspect_mrpack

    from .test_mrpack import URL, range_handler

    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("modrinth.index.json", json.dumps(index))
        zf.writestr("overrides/config/missing_mods_checker.json", json.dumps({"not": "a list"}))
    with respx.mock(assert_all_called=False) as mock:
        mock.get(URL).mock(side_effect=range_handler(buf.getvalue()))
        result = inspect_mrpack(URL, None)
    assert isinstance(result, dict) and "available" in result
    if isinstance(index, dict):
        assert result["available"] is True and result["manual_downloads"] == []


# --- C-2 ------------------------------------------------------------------------------------------------------
def _tshock_with_config(make_instance: Any, fake_agent: Any) -> tuple[Any, str]:
    from apps.instances.spec import rcon_password

    inst = make_tr(
        make_instance,
        "tshock",
        game_port=7778,
        ports=[{"name": "game", "container_port": 7777, "protocol": "tcp", "host_port": 7778}],
    )
    token = rcon_password(inst)
    config = (
        '{\n  "Settings": {\n    "RestApiEnabled": true,\n    "ApplicationRestTokens": {\n'
        f'      "{token}": {{\n        "Username": "mcpanel",\n        "UserGroupName": "superadmin"\n      }}\n'
        "    }\n  }\n}\n"
    )
    fake_agent.put_file(inst.id, "config.json", config)
    return inst, token


def _body(resp: Any) -> bytes:
    return b"".join(resp.streaming_content) if getattr(resp, "streaming", False) else resp.content


def test_c2_search_is_no_oracle_without_configure(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    """Adopted from c-untrusted-data/poc test_c2b_search_is_an_oracle_for_masked_secrets: the Operator recovered the
    32-character token with 676 searches. Now no hit of a secret-bearing file, nor any line with the secret."""
    inst, token = _tshock_with_config(make_instance, fake_agent)
    fake_agent.put_file(inst.id, "notes.txt", f"copied: {token}\nnothing here\n")
    url = f"/api/instances/{inst.id}/files/search"

    def hits(client: Any, query: str) -> list[dict[str, Any]]:
        resp = client.post(url, {"query": query, "path": ""}, format="json")
        assert resp.status_code == 200, resp.content
        return resp.json()["results"]

    operator = clients["operator"]
    assert not [r for r in hits(operator, '": {') if r["path"] == "config.json"]
    for query in (token[-6:].lower(), token[:4].lower(), "copied"):
        assert hits(operator, query) == [], query
    assert [r["path"] for r in hits(operator, "nothing")] == ["notes.txt"]
    # the owner (configure) still finds them, masked
    owner_hits = hits(clients["owner"], '": {')
    assert any(r["path"] == "config.json" and r["text"].startswith('"********"') for r in owner_hits)
    assert token not in str(owner_hits)


def test_c2_a_secret_bearing_file_is_not_renamed_without_configure(
    make_instance: Any, fake_agent: Any, make_user: Any, client_for: Any, clients: Any
) -> None:
    """Adopted from test_c2a_rename_then_download_returns_the_token (a custom role with files.write)."""
    from apps.accounts.models import Role

    inst, token = _tshock_with_config(make_instance, fake_agent)
    role = Role.objects.create(
        key="file-editor", name="File editor", permissions=["instance.view", "instance.files.read", "instance.files.write"]
    )
    friend = client_for(make_user("friend", role=role))
    base = f"/api/instances/{inst.id}/files"
    resp = friend.post(f"{base}/rename", {"source": "config.json", "destination": "notes.txt"}, format="json")
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "managed_file"
    # a copy of the token in any file (here written by hand) is masked in this user's downloads, by value
    fake_agent.put_file(inst.id, "elsewhere/copy.txt", f"x {token} y\n")
    assert token.encode() not in _body(friend.get(f"{base}/download", {"path": "elsewhere/copy.txt"}))
    # the owner may rename it (he can read and change the token anyway)
    resp = clients["owner"].post(f"{base}/rename", {"source": "config.json", "destination": "config.old"}, format="json")
    assert resp.status_code == 200, resp.content


def test_c2_minecraft_rcon_password_is_masked_in_the_file_manager(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    """Adopted from test_c2c_minecraft_rcon_password_is_masked_in_the_properties_tab_only."""
    from apps.instances.spec import rcon_password

    inst = make_instance()
    secret = rcon_password(inst)
    fake_agent.put_file(inst.id, "server.properties", f"motd=hi\nenable-rcon=true\nrcon.password={secret}\n")
    fake_agent.put_file(inst.id, ".rcon-cli.env", f"password={secret}\n")
    operator = clients["operator"]
    base = f"/api/instances/{inst.id}/files"
    assert secret not in str(operator.get(f"/api/instances/{inst.id}/properties").json())
    assert secret not in operator.get(f"{base}/text", {"path": "server.properties"}).json()["content"]
    assert secret.encode() not in _body(operator.get(f"{base}/download", {"path": ".rcon-cli.env"}))
    assert operator.post(f"{base}/search", {"query": "rcon", "path": ""}, format="json").json()["results"] == []
    # saving server.properties from the editor (masked) keeps the password
    shown = clients["owner"].get(f"{base}/text", {"path": "server.properties"}).json()["content"]
    assert "rcon.password=********" in shown
    resp = clients["owner"].put(
        f"{base}/text", {"path": "server.properties", "content": shown.replace("motd=hi", "motd=yo")}, format="json"
    )
    assert resp.status_code == 200, resp.content
    written = fake_agent.text(inst.id, "server.properties")
    assert f"rcon.password={secret}" in written and "motd=yo" in written


def test_r15_saving_any_file_the_editor_masked_keeps_the_rcon_password(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    """R-15 of the branch review: only server.properties got the password back; a plugin config or .rcon-cli.env
    saved from the editor was written with ********."""
    from apps.instances.spec import rcon_password

    inst = make_instance()
    secret = rcon_password(inst)
    fake_agent.put_file(inst.id, "plugins/Votifier/config.yml", f"host: 0.0.0.0\nrcon-pass: {secret}\nport: 8192\n")
    fake_agent.put_file(inst.id, ".rcon-cli.env", f"password={secret}\n")
    fake_agent.put_file(inst.id, "notes.txt", "a typed mask: ********\n")
    base = f"/api/instances/{inst.id}/files"
    owner = clients["owner"]
    for path, edit in (("plugins/Votifier/config.yml", ("8192", "8193")), (".rcon-cli.env", ("password", "password"))):
        shown = owner.get(f"{base}/text", {"path": path}).json()["content"]
        assert secret not in shown and "********" in shown
        resp = owner.put(f"{base}/text", {"path": path, "content": shown.replace(*edit)}, format="json")
        assert resp.status_code == 200, resp.content
        assert secret in fake_agent.text(inst.id, path) and "********" not in fake_agent.text(inst.id, path), path
    assert "8193" in fake_agent.text(inst.id, "plugins/Votifier/config.yml")
    # a file that never held the password keeps a typed mask as it is
    owner.put(f"{base}/text", {"path": "notes.txt", "content": "a typed mask: ********\nmore\n"}, format="json")
    assert fake_agent.text(inst.id, "notes.txt") == "a typed mask: ********\nmore\n"
