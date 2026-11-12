"""Agent fixes of the multi-game review (2026-11-09) besides the claim guard (test_claim_guard.py):

* item 5: ``GET /files/archive`` leaves out ``exclude`` paths (control-secret files);
* item 7: Satisfactory new game / save import never overwrite saves; collision-free save names;
* item 11: the Satisfactory profile runs on the instance network only;
* item 12: ports a variable names cannot collide with the API port, no duplicate port pairs;
* item 14: image VOLUMEs fail closed at create, bind sources re-checked before every start;
* item 15: the console exec is bounded by ``EXEC_TIMEOUT``.
"""

from __future__ import annotations

import io
import threading
import time
import uuid
import zipfile
from types import SimpleNamespace

import pytest

from mcagent import docker_ops, main, profiles
from mcagent.docker_ops import build_create_kwargs
from mcagent.drivers import console_io
from mcagent.drivers import satisfactory as sfd
from mcagent.errors import AgentError
from mcagent.profiles import Mount, factorio, satisfactory
from mcagent.schemas import InstanceSpec

from . import test_games_satisfactory as _sf
from .test_games import _testgame
from .test_games_satisfactory import (
    IID,
    Container,
    claimed,
    driver_for,
    sf_spec,
)

# the Satisfactory tests' fixtures
cert_dir = _sf.cert_dir
fake = _sf.fake
settings = _sf.settings


# --- item 5: archive excludes ---------------------------------------------------------------------------
@pytest.fixture
def server_folder(agent_settings):
    iid = uuid.uuid4()
    folder = agent_settings.instances_dir / str(iid)
    (folder / "config").mkdir(parents=True)
    (folder / "config" / "rconpw").write_text("secret")
    (folder / "config" / "map-settings.json").write_text("{}")
    (folder / ".mcpanel").mkdir()
    (folder / ".mcpanel" / "api-token").write_text("token")
    (folder / "saves").mkdir()
    (folder / "saves" / "world.zip").write_bytes(b"zip")
    return iid


def _names(response) -> set[str]:
    assert response.status_code == 200, response.text
    return set(zipfile.ZipFile(io.BytesIO(response.content)).namelist())


def test_archive_leaves_out_excluded_files(api, auth, server_folder):
    base = f"/v1/instances/{server_folder}/files/archive"
    assert "server/config/rconpw" in _names(api.get(f"{base}?path=", headers=auth))
    names = _names(api.get(f"{base}?path=&exclude=config/rconpw", headers=auth))
    assert "server/config/rconpw" not in names and "server/config/map-settings.json" in names
    # the exclude is relative to the server folder, also when only a subfolder is zipped
    names = _names(api.get(f"{base}?path=config&exclude=config/rconpw", headers=auth))
    assert names == {"config/map-settings.json"}
    # a folder: everything below it
    names = _names(api.get(f"{base}?path=&exclude=.mcpanel&exclude=config/rconpw", headers=auth))
    assert not any(n.startswith("server/.mcpanel/") for n in names) and "server/saves/world.zip" in names
    # a name that only starts like an excluded one stays
    assert "server/config/map-settings.json" in _names(
        api.get(f"{base}?path=&exclude=config/map", headers=auth)
    )


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "config/../../x", "", "."])
def test_archive_refuses_invalid_excludes(api, auth, server_folder, bad):
    r = api.get(
        f"/v1/instances/{server_folder}/files/archive", params={"path": "", "exclude": bad}, headers=auth
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_path"


def test_archive_excludes_are_bounded(api, auth, server_folder):
    params = [("path", "")] + [("exclude", f"x{i}") for i in range(33)]
    r = api.get(f"/v1/instances/{server_folder}/files/archive", params=params, headers=auth)
    assert r.status_code == 400
    params = [("path", "")] + [("exclude", f"x{i}") for i in range(32)]
    assert (
        api.get(f"/v1/instances/{server_folder}/files/archive", params=params, headers=auth).status_code
        == 200
    )


def test_archive_excludes_feature():
    assert "archive_excludes" in main.FEATURES


# --- item 7: saves are never overwritten --------------------------------------------------------------------
def test_save_names_are_collision_free():
    names = {s: sfd.save_name_for(s) for s in ("a b", "a_b", "a-b", "a  b", "a.b", "x" * 70, "x" * 69 + "y")}
    assert len(set(names.values())) == len(names)
    for name in names.values():
        assert sfd.SAVE_NAME_RE.fullmatch(name) and len(name) <= 64 and name.endswith("_mcpanel")
    assert names["a_b"] == "a_b_mcpanel" and names["a-b"] == "a-b_mcpanel"  # existing servers keep theirs
    # a plain name that looks like a hashed one is hashed too, so it never equals another session's
    hashed = sfd.save_name_for("a b")
    tail = hashed[: -len("_mcpanel")]
    assert sfd.save_name_for(tail) != hashed
    assert sfd.save_name_for("x" * 56) == "x" * 56 + "_mcpanel"  # exactly fits
    assert sfd.save_name_for("x" * 57) != "x" * 57 + "_mcpanel"
    assert sfd.save_name_for("!!!").startswith("session_")


def test_new_game_refuses_an_existing_session(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    drv.game_action(settings, c, "new_game", {"session_name": "Ficsit"}, 60)
    fake.requests.clear()
    with pytest.raises(AgentError) as exc:
        drv.game_action(settings, c, "new_game", {"session_name": "Ficsit"}, 60)
    assert exc.value.code == "session_exists" and exc.value.status == 409
    assert "CreateNewGame" not in fake.functions()
    drv.game_action(settings, c, "new_game", {"session_name": "Ficsit two"}, 60)


def _upload_file(settings, name: str = ".mcpanel-import-x.sav") -> str:
    (settings.instances_dir / str(IID) / name).write_bytes(b"save bytes")
    return name


def test_upload_refuses_an_existing_save_file(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    saves = settings.instances_dir / str(IID) / "saved" / "server"
    saves.mkdir(parents=True)
    (saves / "imported.sav").write_bytes(b"the server's own save")
    fake.requests.clear()
    with pytest.raises(AgentError) as exc:
        drv.game_action(
            settings, c, "upload_save", {"path": _upload_file(settings), "save_name": "imported"}, 60
        )
    assert exc.value.code == "save_exists" and exc.value.status == 409
    assert fake.uploads == [] and fake.requests == []
    assert (saves / "imported.sav").read_bytes() == b"the server's own save"
    # a symbolic link in its place counts as existing too
    (saves / "linked.sav").symlink_to(saves / "imported.sav")
    with pytest.raises(AgentError) as exc:
        drv.game_action(
            settings, c, "upload_save", {"path": _upload_file(settings), "save_name": "linked"}, 60
        )
    assert exc.value.code == "save_exists"


def test_upload_and_load_saves_the_running_game_first(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    drv.game_action(settings, c, "new_game", {"session_name": "Ficsit"}, 60)
    fake.requests.clear()
    drv.game_action(
        settings,
        c,
        "upload_save",
        {"path": _upload_file(settings), "save_name": "imported", "load": True},
        60,
    )
    functions = fake.functions()
    assert functions.index("SaveGame") < functions.index("UploadSaveGame")
    saved = next(d for f, d, _l in fake.requests if f == "SaveGame")
    assert saved == {"SaveName": "Ficsit_mcpanel"}
    # the save fails: nothing is uploaded (loading would lose the progress)
    fake.fail["SaveGame"] = (200, {"errorCode": "save_game_failed", "errorMessage": "disk full"})
    fake.uploads.clear()
    with pytest.raises(AgentError):
        drv.game_action(
            settings,
            c,
            "upload_save",
            {"path": _upload_file(settings), "save_name": "other", "load": True},
            60,
        )
    assert fake.uploads == []


def test_upload_without_load_does_not_save(settings, fake):
    drv, c = driver_for(fake), Container(fake.port)
    claimed(settings, fake, drv, c)
    drv.game_action(settings, c, "new_game", {"session_name": "Ficsit"}, 60)
    fake.requests.clear()
    drv.game_action(
        settings, c, "upload_save", {"path": _upload_file(settings), "save_name": "kept", "load": False}, 60
    )
    assert "SaveGame" not in fake.functions()


# --- item 11: the instance network only ------------------------------------------------------------------------
@pytest.mark.parametrize("network", ["host", "macvlan_lan", "other_bridge"])
def test_satisfactory_runs_on_the_instance_network_only(settings, network):
    settings.allowed_networks = ["macvlan_lan", "other_bridge"]
    settings.allow_host_network = True
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, sf_spec(network=network), "h")
    assert exc.value.code == "network_not_supported" and exc.value.status == 400
    assert build_create_kwargs(settings, IID, sf_spec(network=settings.instance_network), "h")["network"] == (
        settings.instance_network
    )


def test_minecraft_keeps_the_allowed_networks(settings):
    settings.allowed_networks = ["other_bridge"]
    spec = InstanceSpec(
        name="S",
        image="itzg/minecraft-server:stable-java21",
        memory_limit_mb=2048,
        rcon_password="x" * 16,
        network="other_bridge",
    )
    assert build_create_kwargs(settings, IID, spec, "h")["network"] == "other_bridge"


def test_apply_refuses_the_network_before_queuing(api, auth, settings):
    r = api.put(
        f"/v1/instances/{IID}/container",
        json={"spec": sf_spec(network="host").model_dump(mode="json")},
        headers=auth,
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "network_not_supported"


# --- item 12: port collisions --------------------------------------------------------------------------------
def test_messaging_port_cannot_be_the_api_port(settings):
    for ports in ([], sf_spec().model_dump()["ports"][:2]):
        with pytest.raises(AgentError) as exc:
            build_create_kwargs(settings, IID, sf_spec(env={"SERVERMESSAGINGPORT": "7777"}, ports=ports), "h")
        assert exc.value.code == "port_collision" and exc.value.status == 400


def test_messaging_host_port_cannot_be_the_api_host_port(settings):
    ports = [
        {"container_port": 7777, "host_port": 29010, "protocol": "tcp"},
        {"container_port": 7777, "host_port": 29010, "protocol": "udp"},
        # same number and protocol on another address: the schema's own check (per address) passes
        {"container_port": 29011, "host_port": 29010, "protocol": "tcp", "host_ip": "127.0.0.1"},
    ]
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, sf_spec(ports=ports), "h")
    assert exc.value.code == "port_collision"


def test_a_container_port_is_published_once(settings):
    ports = [
        {"container_port": 7777, "host_port": 29010, "protocol": "udp"},
        {"container_port": 7777, "host_port": 29012, "protocol": "udp"},
    ]
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, sf_spec(ports=ports), "h")
    assert exc.value.code == "port_collision"
    ports = [  # the same host port on two addresses: still one port of this host
        {"container_port": 7777, "host_port": 29010, "protocol": "udp", "host_ip": "0.0.0.0"},
        {"container_port": 29011, "host_port": 29010, "protocol": "udp", "host_ip": "127.0.0.1"},
    ]
    with pytest.raises(AgentError):
        build_create_kwargs(settings, IID, sf_spec(ports=ports), "h")
    build_create_kwargs(settings, IID, sf_spec(), "h")  # the regular spec


def test_minecraft_port_lists_are_unchanged(settings):
    spec = InstanceSpec(
        name="S",
        image="itzg/minecraft-server:stable-java21",
        memory_limit_mb=2048,
        rcon_password="x" * 16,
        ports=[
            {"container_port": 25565, "host_port": 25565, "host_ip": "0.0.0.0"},
            {"container_port": 25565, "host_port": 25565, "host_ip": "::"},
        ],
    )
    assert build_create_kwargs(settings, IID, spec, "h")["ports"] == {
        "25565/tcp": [("0.0.0.0", 25565), ("::", 25565)]
    }


# --- item 14: image volumes and bind sources --------------------------------------------------------------------
class _Images:
    def __init__(self, volumes):
        self.volumes = volumes

    def get(self, _name):
        return SimpleNamespace(attrs={"Config": {"Volumes": self.volumes}})


class _CreatingClient:
    def __init__(self, volumes):
        self.images = _Images(volumes)
        self.created: dict = {}
        self.api = SimpleNamespace(api_version="1.47", create_container=self._create)
        self.containers = SimpleNamespace(get=lambda cid: SimpleNamespace(id=cid))

    def _create(self, **kwargs):
        self.created = kwargs
        return {"Id": "new"}


def test_a_volume_without_a_mount_fails_closed(settings, monkeypatch):
    fake = _CreatingClient({"/": {}, "/config": {}})  # "/" gets no mount of its own
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    monkeypatch.setattr(docker_ops, "ensure_network", lambda _s: None)
    with pytest.raises(AgentError) as exc:
        docker_ops.create_container(settings, IID, sf_spec(ports=[]), "h")
    assert exc.value.code == "image_volume_uncovered" and fake.created == {}
    fake.images.volumes = {"/config": {}, "/opt/cache": {}}  # mapped into .volumes
    docker_ops.create_container(settings, IID, sf_spec(ports=[]), "h")
    assert fake.created


def test_a_profile_without_volume_mounting_refuses_uncovered_volumes(settings, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES", dict(profiles.PROFILES))
    monkeypatch.delenv("AGENT_GAME_TESTGAME_IMAGES", raising=False)
    profiles.register(_testgame())
    assert profiles.get("testgame").mount_image_volumes is False
    spec = InstanceSpec(
        name="T", image="example/testgame:1", memory_limit_mb=1024, rcon_password="x" * 16, game="testgame"
    )
    fake = _CreatingClient({"/srv/game": {}, "/var/lib/extra": {}})
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    monkeypatch.setattr(docker_ops, "ensure_network", lambda _s: None)
    (settings.instances_dir / str(IID) / "saves").mkdir()
    with pytest.raises(AgentError) as exc:
        docker_ops.create_container(settings, IID, spec, "h")
    assert exc.value.code == "image_volume_uncovered" and "/var/lib/extra" in str(exc.value)
    fake.images.volumes = {"/srv/game": {}, "/srv/game/saves": {}}  # both are profile mounts
    docker_ops.create_container(settings, IID, spec, "h")
    assert fake.created


def test_minecraft_create_still_never_inspects_the_image(settings, monkeypatch):
    class NoImages(_CreatingClient):
        @property
        def images(self):
            raise AssertionError("Minecraft's image is not inspected")

        @images.setter
        def images(self, _v):
            pass

    fake = NoImages({})
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    monkeypatch.setattr(docker_ops, "ensure_network", lambda _s: None)
    spec = InstanceSpec(
        name="S", image="itzg/minecraft-server:stable-java21", memory_limit_mb=2048, rcon_password="x" * 16
    )
    docker_ops.create_container(settings, IID, spec, "h")
    assert fake.created


class _Stopped:
    def __init__(self, binds, labels=None):
        self.id, self.name = "c1", docker_ops.container_name(IID)
        self.labels = {
            docker_ops.LABEL_MANAGED: "true",
            docker_ops.LABEL_INSTANCE: str(IID),
            **(labels or {}),
        }
        self.attrs = {
            "HostConfig": {"Binds": binds, "NetworkMode": "mcpanel_instances"},
            "Mounts": [],
            "Config": {"Image": "x"},
            "State": {"Status": "exited", "Running": False},
        }
        self.started = 0

    def start(self):
        self.started += 1

    def reload(self):
        pass


@pytest.fixture
def factorio_folder(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(docker_ops, "reattach_recreated_networks", lambda _c, *_a: [])
    host = docker_ops.host_instance_dir(settings, IID)
    folder = settings.instances_dir / str(IID)
    (folder / ".volumes" / "opt_cache").mkdir(parents=True)
    binds = [
        f"{host}:/factorio:rw",
        f"{host / '.volumes/opt_cache'}:/opt/cache:rw",
        "/etc/ssl/ca.crt:/mcpanel/certs/ca-bundle.crt:ro",
    ]
    c = _Stopped(binds, {"io.mcpanel.game": "factorio"})
    monkeypatch.setattr(docker_ops, "get_container", lambda _iid: c)
    return folder, c


def test_start_rechecks_the_image_volume_folders(api, auth, settings, factorio_folder, tmp_path):
    folder, c = factorio_folder
    assert api.post(f"/v1/instances/{IID}/start", headers=auth).status_code == 200
    assert c.started == 1
    target = tmp_path / "host-secrets"
    target.mkdir()
    (folder / ".volumes" / "opt_cache").rmdir()
    (folder / ".volumes" / "opt_cache").symlink_to(target)  # Docker would follow it at the start
    r = api.post(f"/v1/instances/{IID}/start", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "volume_source_invalid"
    assert c.started == 1
    (folder / ".volumes" / "opt_cache").unlink()
    (folder / ".volumes" / "opt_cache").write_text("a file")
    r = api.post(f"/v1/instances/{IID}/start", headers=auth)
    assert r.status_code == 409 and c.started == 1
    (folder / ".volumes" / "opt_cache").unlink()
    r = api.post(f"/v1/instances/{IID}/start", headers=auth)  # gone: Docker would create it as root
    assert r.status_code == 409 and c.started == 1


def test_a_symlinked_volumes_folder_is_refused(settings, factorio_folder, tmp_path, monkeypatch):
    import shutil

    folder, c = factorio_folder
    monkeypatch.setattr(docker_ops, "get_settings", lambda: settings)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "opt_cache").mkdir(parents=True)
    shutil.rmtree(folder / ".volumes")
    (folder / ".volumes").symlink_to(elsewhere)
    with pytest.raises(AgentError) as exc:
        docker_ops.start(IID)
    assert exc.value.code == "volume_source_invalid" and c.started == 0


def test_restart_checks_the_folders_before_it_stops(api, auth, settings, factorio_folder, monkeypatch):
    folder, _c = factorio_folder
    (folder / ".volumes" / "opt_cache").rmdir()
    stops: list = []
    monkeypatch.setattr(docker_ops, "stop", lambda *a, **k: stops.append(a) or docker_ops.StopResult())
    r = api.post(f"/v1/instances/{IID}/restart", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "volume_source_invalid" and stops == []


def test_the_profile_mount_of_a_subfolder_is_checked_too(settings, monkeypatch):
    host = docker_ops.host_instance_dir(settings, IID)
    c = _Stopped([f"{host}:/srv/game:rw", f"{host / 'saves'}:/srv/game/saves:rw"])
    with pytest.raises(AgentError):
        docker_ops.check_bind_sources(settings, IID, c)  # saves/ missing
    (settings.instances_dir / str(IID) / "saves").mkdir()
    docker_ops.check_bind_sources(settings, IID, c)
    assert Mount("saves", "/srv/game/saves") in _testgame().mounts
    assert factorio.PROFILE.mount_image_volumes and satisfactory.PROFILE.mount_image_volumes


# --- item 15: the console exec is bounded ---------------------------------------------------------------------
class _BlockingApi:
    def __init__(self):
        self.release = threading.Event()

    def exec_create(self, container, cmd, **kw):
        return {"Id": "e1"}

    def exec_start(self, exec_id, **kw):
        self.release.wait(10)  # a program that hangs
        return b""

    def exec_inspect(self, exec_id):
        return {"ExitCode": 0}


def test_a_hanging_exec_times_out_and_frees_the_console(monkeypatch):
    api = _BlockingApi()
    monkeypatch.setattr(console_io, "_docker_api", lambda: api)
    monkeypatch.setattr(console_io, "EXEC_TIMEOUT", 0.3)
    c = SimpleNamespace(id="c1")
    started = time.monotonic()
    try:
        with pytest.raises(AgentError) as exc:
            with console_io.console_lock("srv-exec"):
                console_io.exec_line(c, ("inject", "{line}"), "say hi", user="1:1")
        assert exc.value.code == "console_timeout" and exc.value.status == 504
        assert time.monotonic() - started < 3
        with console_io.console_lock("srv-exec", wait=0.1):  # not left held
            pass
    finally:
        api.release.set()


# --- B-12 (security review 2026-11-11): the exec console's read is closed at the timeout and capped ---------
class _StreamingApi:
    """exec_start of a double that yields chunks (not a socket): an iterator over the exec's output that blocks until the
    program prints more, and ends when its connection is closed."""

    def __init__(self, chunks=(), hang=True):
        self.chunks = list(chunks)
        self.hang = hang
        self.closed = threading.Event()
        self.reader_done = threading.Event()

    def exec_create(self, container, cmd, **kw):
        return {"Id": "e1"}

    def exec_start(self, exec_id, **kw):
        assert kw.get("socket") is True
        api = self

        class Stream:
            def __iter__(self):
                try:
                    yield from api.chunks
                    if api.hang:
                        api.closed.wait(30)  # a program that holds its output open
                        if api.closed.is_set():
                            raise OSError("connection closed")
                finally:
                    api.reader_done.set()

            def close(self):
                api.closed.set()

        return Stream()

    def exec_inspect(self, exec_id):
        return {"ExitCode": 0}


def test_a_timed_out_exec_leaves_no_thread_behind(monkeypatch):
    api = _StreamingApi([b"partial"])
    monkeypatch.setattr(console_io, "_docker_api", lambda: api)
    monkeypatch.setattr(console_io, "EXEC_TIMEOUT", 0.3)
    with pytest.raises(AgentError) as exc:
        console_io.exec_line(SimpleNamespace(id="c1"), ("inject", "{line}"), "say hi", user="1:1")
    assert exc.value.code == "console_timeout"
    assert api.closed.is_set() and api.reader_done.wait(2), "the worker's read must end with the request"


def test_exec_output_is_capped_while_it_is_read(monkeypatch):
    monkeypatch.setattr(console_io, "EXEC_OUTPUT_MAX", 1000)
    api = _StreamingApi([b"a" * 600] * 20 + [b"END"], hang=False)
    monkeypatch.setattr(console_io, "_docker_api", lambda: api)
    out = console_io.exec_line(SimpleNamespace(id="c1"), ("inject", "{line}"), "say hi", user="1:1")
    assert len(out) == 1000 and out.endswith("END")


# --- R-14: the exec is read through its socket, which the timeout shuts down -------------------------------
class _SocketApi:
    """exec_start(socket=True) as docker-py answers it: the connection's socket, carrying Docker's
    multiplexed frames (adopted from audit-2026-11-09/fixes/review/agent-misc/test_b12_real_docker_stream.py)."""

    def __init__(self, frames: bytes = b""):
        import socket as _socket

        self.a, self.b = _socket.socketpair()
        if frames:
            self.b.sendall(frames)

    def exec_create(self, container, cmd, **kw):
        return {"Id": "e1"}

    def exec_start(self, exec_id, **kw):
        assert kw.get("socket") is True and not kw.get("stream")
        return self.a

    def exec_inspect(self, exec_id):
        return {"ExitCode": 0}


def _frame(stream: int, data: bytes) -> bytes:
    import struct

    return struct.pack(">BxxxL", stream, len(data)) + data


def test_a_timed_out_exec_on_a_real_socket_leaves_no_thread_behind(monkeypatch):
    api = _SocketApi()  # the program holds its output open: the read blocks in recv
    monkeypatch.setattr(console_io, "_docker_api", lambda: api)
    monkeypatch.setattr(console_io, "EXEC_TIMEOUT", 0.3)
    before = {t.ident for t in threading.enumerate()}
    with pytest.raises(AgentError) as exc:
        console_io.exec_line(SimpleNamespace(id="c1"), ("inject", "{line}"), "say hi", user="1:1")
    assert exc.value.code == "console_timeout"
    left = [t for t in threading.enumerate() if t.ident not in before and t.name == "console-exec"]
    for t in left:
        t.join(2)
    assert not any(t.is_alive() for t in left), "the worker's read must end with the request"
    api.b.close()


def test_exec_output_is_demultiplexed_from_the_socket(monkeypatch):
    api = _SocketApi(_frame(1, b"Saved the game\n") + _frame(2, b"warn\n"))
    api.b.shutdown(__import__("socket").SHUT_WR)  # the program ended
    monkeypatch.setattr(console_io, "_docker_api", lambda: api)
    out = console_io.exec_line(SimpleNamespace(id="c1"), ("inject", "{line}"), "save", user="1:1")
    assert out == "Saved the game\nwarn\n"
    api.b.close()
