"""Mods/plugins content manager (apps.content).

Modrinth is simulated with respx (no real network); the node agent is replaced by an
in-memory fake patched onto ``AgentClient``. Fixtures live in this module on purpose
(they are specific to the content manager and prefixed ``ct_``).
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import re
import zipfile
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest
import respx
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.audit.models import AuditEvent
from apps.content import service
from apps.nodes.client import AgentClient, AgentError

MR = "https://api.modrinth.com/v2"
CDN = "https://cdn.modrinth.com"
ALLOWED_HOSTS = {"cdn.modrinth.com", "cdn-raw.modrinth.com"}


# ======================================================================================
# Fake Modrinth (served through respx)
# ======================================================================================
class FakeModrinth:
    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, dict[str, Any]] = {}
        self.search_hits: list[dict[str, Any]] = []
        self.search_params: list[dict[str, Any]] = []
        self.update_bodies: list[dict[str, Any]] = []
        self.ignore_version_filters = False
        self.fail_identify = False

    # -- data --------------------------------------------------------------------------
    def project(self, pid: str, slug: str, title: str, loaders: list[str], **extra: Any) -> dict[str, Any]:
        p = {
            "id": pid,
            "slug": slug,
            "title": title,
            "description": f"{title} description",
            "project_type": extra.pop("project_type", "mod"),
            "server_side": extra.pop("server_side", "required"),
            "client_side": extra.pop("client_side", "optional"),
            "loaders": loaders,
            "game_versions": ["1.20.1", "1.20.4"],
            "icon_url": f"{CDN}/data/{pid}/icon.png",
            "license": {"id": "MIT"},
            **extra,
        }
        self.projects[pid] = p
        return p

    def version(
        self,
        vid: str,
        pid: str,
        number: str,
        *,
        loaders: list[str],
        game_versions: list[str],
        published: str,
        version_type: str = "release",
        filename: str | None = None,
        deps: list[dict[str, Any]] | None = None,
        url_host: str = "cdn.modrinth.com",
    ) -> dict[str, Any]:
        filename = filename or f"{self.projects[pid]['slug']}-{number}.jar"
        v = {
            "id": vid,
            "project_id": pid,
            "name": f"{self.projects[pid]['title']} {number}",
            "version_number": number,
            "version_type": version_type,
            "status": "listed",
            "loaders": loaders,
            "game_versions": game_versions,
            "date_published": published,
            "downloads": 100,
            "files": [
                {
                    "hashes": {"sha1": self.sha1(vid), "sha512": hashlib.sha512(vid.encode()).hexdigest()},
                    "url": f"https://{url_host}/data/{pid}/versions/{vid}/{quote(filename)}",
                    "filename": filename,
                    "primary": True,
                    "size": 4096,
                }
            ],
            "dependencies": deps or [],
        }
        self.versions[vid] = v
        return v

    @staticmethod
    def sha1(vid: str) -> str:
        return hashlib.sha1(vid.encode()).hexdigest()

    def hit(self, pid: str) -> dict[str, Any]:
        p = self.projects[pid]
        return {
            "project_id": pid,
            "project_type": p["project_type"],
            "slug": p["slug"],
            "title": p["title"],
            "description": p["description"],
            "author": "someone",
            "categories": p["loaders"],
            "versions": p["game_versions"],
            "downloads": 10,
            "follows": 1,
            "icon_url": p["icon_url"],
            "date_modified": "2024-01-01T00:00:00Z",
            "server_side": p["server_side"],
            "client_side": p["client_side"],
            "license": "MIT",
        }

    def _by_hash(self, sha1: str) -> dict[str, Any] | None:
        return next((v for v in self.versions.values() if v["files"][0]["hashes"]["sha1"] == sha1), None)

    def _matching(
        self, pid: str, loaders: list[str] | None, game_versions: list[str] | None, types: list[str] | None = None
    ) -> list[dict[str, Any]]:
        out = []
        for v in self.versions.values():
            if v["project_id"] != pid:
                continue
            if not self.ignore_version_filters:
                if loaders and not set(loaders) & set(v["loaders"]):
                    continue
                if game_versions and not set(game_versions) & set(v["game_versions"]):
                    continue
            if types and v["version_type"] not in types:
                continue
            out.append(v)
        return sorted(out, key=lambda v: v["date_published"], reverse=True)

    # -- routes ------------------------------------------------------------------------
    def mount(self, router: respx.MockRouter) -> None:
        router.get(url__regex=rf"^{re.escape(MR)}/project/(?P<pid>[^/?]+)/version(\?.*)?$").mock(side_effect=self._versions)
        router.get(url__regex=rf"^{re.escape(MR)}/project/(?P<pid>[^/?]+)(\?.*)?$").mock(side_effect=self._project)
        router.get(url__regex=rf"^{re.escape(MR)}/version/(?P<vid>[^/?]+)(\?.*)?$").mock(side_effect=self._version)
        router.get(url__regex=rf"^{re.escape(MR)}/projects(\?.*)?$").mock(side_effect=self._projects)
        router.get(url__regex=rf"^{re.escape(MR)}/search(\?.*)?$").mock(side_effect=self._search)
        router.post(f"{MR}/version_files/update").mock(side_effect=self._update)
        router.post(f"{MR}/version_files").mock(side_effect=self._identify)

    def _resolve(self, key: str) -> dict[str, Any] | None:
        return self.projects.get(key) or next((p for p in self.projects.values() if p["slug"] == key), None)

    def _project(self, request: httpx.Request, pid: str) -> httpx.Response:
        p = self._resolve(pid)
        return httpx.Response(200, json=p) if p else httpx.Response(404)

    def _versions(self, request: httpx.Request, pid: str) -> httpx.Response:
        p = self._resolve(pid)
        if not p:
            return httpx.Response(404)
        q = parse_qs(urlparse(str(request.url)).query)
        loaders = json.loads(q["loaders"][0]) if "loaders" in q else None
        game_versions = json.loads(q["game_versions"][0]) if "game_versions" in q else None
        return httpx.Response(200, json=self._matching(p["id"], loaders, game_versions))

    def _version(self, request: httpx.Request, vid: str) -> httpx.Response:
        v = self.versions.get(vid)
        return httpx.Response(200, json=v) if v else httpx.Response(404)

    def _projects(self, request: httpx.Request) -> httpx.Response:
        ids = json.loads(parse_qs(urlparse(str(request.url)).query)["ids"][0])
        return httpx.Response(200, json=[self.projects[i] for i in ids if i in self.projects])

    def _search(self, request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}
        q["facets"] = json.loads(q["facets"])
        self.search_params.append(q)
        return httpx.Response(200, json={"hits": self.search_hits, "offset": 0, "limit": 20, "total_hits": len(self.search_hits)})

    def _identify(self, request: httpx.Request) -> httpx.Response:
        if self.fail_identify:
            return httpx.Response(503)
        body = json.loads(request.content)
        assert body["algorithm"] == "sha1"
        out = {h: v for h in body["hashes"] if (v := self._by_hash(h))}
        return httpx.Response(200, json=out)

    def _update(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.update_bodies.append(body)
        out = {}
        for h in body["hashes"]:
            cur = self._by_hash(h)
            if not cur:
                continue
            matching = self._matching(cur["project_id"], body["loaders"], body["game_versions"], body.get("version_types"))
            if matching:
                out[h] = matching[0]
        return httpx.Response(200, json=out)


# ======================================================================================
# Fake node agent (content-related file operations only)
# ======================================================================================
class ContentAgent:
    METHODS = ("hash_files", "fetch_file", "stat_file", "rename", "delete_files", "mkdir", "upload")

    def __init__(self) -> None:
        self.files: dict[tuple[str, str], dict[str, Any]] = {}
        self.dirs: set[tuple[str, str]] = set()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_fetch_urls: set[str] = set()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in self.METHODS:
            impl = getattr(self, name)

            def method(_client: Any, *args: Any, _impl: Any = impl, _name: str = name, **kwargs: Any) -> Any:
                self.calls.append((_name, {"args": args, **kwargs}))
                return _impl(*args, **kwargs)

            monkeypatch.setattr(AgentClient, name, method)

    def put(self, instance: Any, path: str, sha1: str, size: int = 2048) -> None:
        self.files[(str(instance.id), path)] = {"sha1": sha1, "size": size, "type": "file"}

    def names(self, instance: Any, directory: str) -> list[str]:
        prefix = f"{directory}/"
        return sorted(p[len(prefix) :] for (iid, p) in self.files if iid == str(instance.id) and p.startswith(prefix))

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]

    # -- AgentClient surface -------------------------------------------------------------
    def hash_files(self, instance_id: Any, path: str, algorithm: str = "sha1") -> list[dict[str, Any]]:
        assert algorithm == "sha1"
        out = []
        for (iid, p), meta in sorted(self.files.items()):
            if iid != str(instance_id) or not p.startswith(f"{path}/") or "/" in p[len(path) + 1 :]:
                continue
            name = p[len(path) + 1 :]
            out.append(
                {"name": name, "path": p, "type": meta["type"], "size": meta["size"], "modified": 1.7e9, "hash": meta["sha1"]}
            )
        return out

    def fetch_file(
        self,
        instance_id: Any,
        *,
        url: str,
        path: str,
        sha1: str | None = None,
        sha512: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        host = urlparse(url).hostname
        if urlparse(url).scheme != "https" or host not in ALLOWED_HOSTS:
            raise AgentError("Download URL is not on an allowed provider host", "url_not_allowed", 403)
        if not (sha1 or sha512):
            raise AgentError("A sha1 or sha512 hash is required to verify downloads", "hash_required", 400)
        if url in self.fail_fetch_urls:
            raise AgentError("Downloaded file failed SHA-1 verification", "hash_mismatch", 502)
        key = (str(instance_id), path)
        if key in self.files and not overwrite:
            raise AgentError("File already exists", "exists", 409)
        self.files[key] = {"sha1": sha1, "size": 4096, "type": "file"}
        return {"path": path, "size": 4096, "sha1": sha1, "sha512": sha512}

    def stat_file(self, instance_id: Any, path: str) -> dict[str, Any]:
        meta = self.files.get((str(instance_id), path))
        if meta is None:
            raise AgentError("Does not exist", "not_found", 404)
        return {"name": path.rsplit("/", 1)[-1], "path": path, "type": meta["type"], "size": meta["size"]}

    def rename(self, instance_id: Any, source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
        src, dst = (str(instance_id), source), (str(instance_id), destination)
        if src not in self.files:
            raise AgentError("Source does not exist", "not_found", 404)
        if dst in self.files and not overwrite:
            raise AgentError("Destination already exists", "exists", 409)
        self.files[dst] = self.files.pop(src)
        return {"source": source, "destination": destination}

    def delete_files(self, instance_id: Any, paths: list[str], recursive: bool = False) -> dict[str, Any]:
        for p in paths:
            if (str(instance_id), p) not in self.files:
                raise AgentError("Does not exist", "not_found", 404)
            del self.files[(str(instance_id), p)]
        return {"deleted": paths}

    def mkdir(self, instance_id: Any, path: str, parents: bool = False) -> dict[str, Any]:
        self.dirs.add((str(instance_id), path))
        return {"path": path}

    def upload(self, instance_id: Any, path: str, chunks: Any, overwrite: bool = False) -> dict[str, Any]:
        key = (str(instance_id), path)
        if key in self.files and not overwrite:
            raise AgentError("File already exists", "exists", 409)
        data = chunks if isinstance(chunks, bytes) else b"".join(chunks)
        self.files[key] = {"sha1": hashlib.sha1(data).hexdigest(), "size": len(data), "type": "file"}
        return {"path": path, "size": len(data)}


# ======================================================================================
# Fixtures
# ======================================================================================
@pytest.fixture
def mr() -> Any:
    fake = FakeModrinth()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        fake.mount(router)
        yield fake


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> ContentAgent:
    fake = ContentAgent()
    fake.install(monkeypatch)
    return fake


@pytest.fixture(autouse=True)
def _ct_clear_cache() -> Any:
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def ct_node(db: Any) -> Any:
    from apps.nodes.models import Node

    return Node.objects.create(name="ct-node", agent_url="http://agent.invalid:8700", is_default=True, use_settings_token=True)


_ports = itertools.count(46000)


@pytest.fixture
def make_server(ct_node: Any) -> Any:
    from apps.instances.models import Instance

    def _make(server_type: str = "PAPER", minecraft_version: str = "1.20.4", **fields: Any) -> Instance:
        return Instance.objects.create(
            name=f"ct-{server_type.lower()}",
            node=ct_node,
            server_type=server_type,
            minecraft_version=minecraft_version,
            image="itzg/minecraft-server:java21",
            game_port=next(_ports),
            **fields,
        )

    return _make


@pytest.fixture
def owner(db: Any) -> Any:
    from apps.accounts.models import User

    return User.objects.create_user(username="ct-owner", password="x-Correct-Horse-1", is_owner=True)


@pytest.fixture
def api(owner: Any) -> APIClient:
    client = APIClient()
    client.force_authenticate(owner)
    return client


def granted_client(instance: Any, codes: list[str], username: str) -> APIClient:
    from apps.accounts.models import InstanceAccess, Role, User

    role = Role.objects.create(key=f"ct-{username}", name=username, permissions=codes)
    user = User.objects.create_user(username=username, password="x-Correct-Horse-1")
    InstanceAccess.objects.create(user=user, instance=instance, role=role)
    client = APIClient()
    client.force_authenticate(user)
    return client


def url(instance: Any, suffix: str = "") -> str:
    return f"/api/instances/{instance.id}/content/{suffix}"


def make_jar(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def seed_luckperms(mr: FakeModrinth) -> None:
    """A multi-platform project (like the real LuckPerms) with plugin and mod builds."""
    mr.project("LPerm001", "luckperms", "LuckPerms", ["bukkit", "paper", "spigot", "folia", "fabric"])
    mr.version(
        "LPold001",
        "LPerm001",
        "5.4.0-bukkit",
        loaders=["bukkit", "paper", "spigot"],
        game_versions=["1.20.4"],
        published="2024-01-01T00:00:00Z",
    )
    mr.version(
        "LPnew001",
        "LPerm001",
        "5.4.9-bukkit",
        loaders=["bukkit", "paper", "spigot", "folia"],
        game_versions=["1.20.4"],
        published="2024-03-01T00:00:00Z",
    )
    mr.version(
        "LPbeta01",
        "LPerm001",
        "5.5.0-beta",
        loaders=["bukkit", "paper", "spigot"],
        game_versions=["1.20.4"],
        published="2024-04-01T00:00:00Z",
        version_type="beta",
    )
    mr.version(
        "LPfab001", "LPerm001", "5.4.9-fabric", loaders=["fabric"], game_versions=["1.20.4"], published="2024-03-02T00:00:00Z"
    )
    mr.version(
        "LPmc1201",
        "LPerm001",
        "5.4.9-old",
        loaders=["bukkit", "paper", "spigot"],
        game_versions=["1.20.1"],
        published="2024-03-03T00:00:00Z",
    )


# ======================================================================================
# Validation helpers
# ======================================================================================
@pytest.mark.parametrize(
    "name",
    [
        "",
        "../evil.jar",
        "mods/evil.jar",
        "a\\b.jar",
        "evil.txt",
        ".jar",
        "evil.jar ",
        " evil.jar",
        "e\x00vil.jar",
        "evil.JAR",
        "..",
        "x" * 300 + ".jar",
    ],
)
def test_validate_filename_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(service.ApiError) as exc:
        service.validate_filename(name)
    assert exc.value.error_code == "invalid_filename"


def test_validate_filename_accepts_plain_jars() -> None:
    assert service.validate_filename("Lucky Perms+1.0.jar") == "Lucky Perms+1.0.jar"
    assert service.validate_filename("x.jar.disabled") == "x.jar.disabled"
    with pytest.raises(service.ApiError):
        service.validate_filename("x.jar.disabled", allow_disabled=False)


@pytest.mark.parametrize("value", ["../x", "a/b", "a?b=1", "", "x" * 65, "-abc"])
def test_validate_project_id_rejects_path_tricks(value: str) -> None:
    with pytest.raises(service.ApiError):
        service.validate_project_id(value)


# ======================================================================================
# Context / server types
# ======================================================================================
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("server_type", "loader", "kind", "loaders"),
    [
        ("PAPER", "", "plugins", ["paper", "spigot", "bukkit"]),
        ("PURPUR", "", "plugins", ["purpur", "paper", "spigot", "bukkit"]),
        ("FOLIA", "", "plugins", ["folia"]),
        ("FABRIC", "", "mods", ["fabric"]),
        ("QUILT", "", "mods", ["quilt", "fabric"]),
        ("FORGE", "", "mods", ["forge"]),
        ("NEOFORGE", "", "mods", ["neoforge"]),
        ("MODRINTH", "fabric", "mods", ["fabric"]),
        ("AUTO_CURSEFORGE", "forge", "mods", ["forge"]),
    ],
)
def test_context_per_server_type(make_server: Any, server_type: str, loader: str, kind: str, loaders: list[str]) -> None:
    ctx = service.context_for(make_server(server_type, modpack_loader=loader))
    assert ctx.kind == kind
    assert ctx.directory == kind
    assert list(ctx.loaders) == loaders
    assert ctx.minecraft_version == "1.20.4"


@pytest.mark.django_db
@pytest.mark.parametrize("server_type", ["VANILLA", "CUSTOM"])
def test_unsupported_server_types_get_clear_error(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth, server_type: str
) -> None:
    inst = make_server(server_type)
    for method, suffix, body in [
        ("get", "", None),
        ("get", "search?q=x", None),
        ("post", "install", {"project_id": "abc"}),
        ("post", "toggle", {"filename": "a.jar", "enabled": False}),
    ]:
        resp = getattr(api, method)(url(inst, suffix), body, format="json") if body else getattr(api, method)(url(inst, suffix))
        assert resp.status_code == 409, (suffix, resp.content)
        assert resp.json()["error"]["code"] == "content_unsupported"
        assert resp.json()["error"]["message"] == "This server type does not support mods or plugins"
    assert agent.calls == []


@pytest.mark.django_db
def test_modpack_without_known_loader_is_refused(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    inst = make_server("AUTO_CURSEFORGE", modpack_loader="")
    resp = api.get(url(inst))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "loader_unknown"


# ======================================================================================
# Inventory
# ======================================================================================
@pytest.mark.django_db
def test_inventory_identifies_and_classifies_files(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    seed_luckperms(mr)
    inst = make_server("PAPER")
    agent.put(inst, "plugins/LuckPerms-5.4.0.jar", mr.sha1("LPold001"), size=1111)
    agent.put(inst, "plugins/Homemade.jar", hashlib.sha1(b"homemade").hexdigest())
    agent.put(inst, "plugins/Old.jar.disabled", hashlib.sha1(b"old").hexdigest())
    agent.put(inst, "plugins/config.yml", hashlib.sha1(b"cfg").hexdigest())
    agent.put(inst, "plugins/LuckPerms/config.yml", hashlib.sha1(b"nested").hexdigest())

    resp = api.get(url(inst))
    assert resp.status_code == 200, resp.content
    data = resp.json()
    assert data["kind"] == "plugins" and data["directory"] == "plugins"
    assert data["loaders"] == ["paper", "spigot", "bukkit"]
    assert data["identification"] == {"available": True, "error": None}
    files = {f["filename"]: f for f in data["files"]}
    assert set(files) == {"LuckPerms-5.4.0.jar", "Homemade.jar", "Old.jar.disabled"}
    lp = files["LuckPerms-5.4.0.jar"]
    assert lp["source"] == "modrinth" and lp["enabled"] is True and lp["size"] == 1111
    assert lp["project"]["title"] == "LuckPerms" and lp["project"]["id"] == "LPerm001"
    assert lp["version"]["id"] == "LPold001" and lp["version"]["version_number"] == "5.4.0-bukkit"
    assert lp["compatibility"] == {"loader": True, "minecraft": True}
    assert files["Homemade.jar"]["source"] == "manual" and files["Homemade.jar"]["project"] is None
    assert files["Old.jar.disabled"]["enabled"] is False
    assert data["summary"] == {"total": 3, "enabled": 2, "disabled": 1, "identified": 1, "manual": 2}
    assert agent.called("hash_files")[0]["args"][1] == "plugins"
    # the inventory is also served without the trailing slash (not by the instance-action route)
    no_slash = api.get(f"/api/instances/{inst.id}/content")
    assert no_slash.status_code == 200, no_slash.content
    assert no_slash.json()["summary"] == data["summary"]


@pytest.mark.django_db
def test_inventory_modpack_warning_and_mods_dir(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    inst = make_server("MODRINTH", minecraft_version="1.20.1", modpack_loader="fabric")
    data = api.get(url(inst)).json()
    assert data["kind"] == "mods" and data["is_modpack"] is True
    assert any("modpack" in w and "replaced" in w for w in data["warnings"])
    assert agent.called("hash_files")[0]["args"][1] == "mods"


@pytest.mark.django_db
def test_inventory_survives_modrinth_outage(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    mr.fail_identify = True
    inst = make_server("FABRIC")
    agent.put(inst, "mods/a.jar", hashlib.sha1(b"a").hexdigest())
    resp = api.get(url(inst))
    assert resp.status_code == 200
    data = resp.json()
    assert data["identification"]["available"] is False
    assert data["files"][0]["source"] == "unknown"
    assert any("Could not identify" in w for w in data["warnings"])


@pytest.mark.django_db
def test_inventory_on_unpinned_version_warns(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    inst = make_server("PAPER", minecraft_version="LATEST")
    data = api.get(url(inst)).json()
    assert data["minecraft_version"] is None
    assert any("newest Minecraft version" in w for w in data["warnings"])
    resp = api.get(url(inst, "search?q=lp"))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "minecraft_version_unpinned"


# ======================================================================================
# Search / versions
# ======================================================================================
@pytest.mark.django_db
def test_search_plugins_uses_plugin_facets_and_marks_installed(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    seed_luckperms(mr)
    mr.project("Other001", "other", "Other", ["paper"])
    mr.search_hits = [mr.hit("LPerm001"), mr.hit("Other001")]
    inst = make_server("PAPER")
    agent.put(inst, "plugins/lp.jar.disabled", mr.sha1("LPold001"))

    resp = api.get(url(inst, "search?q=luck&offset=20&sort=downloads"))
    assert resp.status_code == 200, resp.content
    params = mr.search_params[-1]
    assert params["query"] == "luck" and params["offset"] == "20" and params["index"] == "downloads"
    facets = params["facets"]
    assert ["project_type:plugin"] in facets
    assert ["categories:paper", "categories:spigot", "categories:bukkit"] in facets
    assert ["versions:1.20.4"] in facets
    assert ["server_side:required", "server_side:optional"] in facets
    results = {r["id"]: r for r in resp.json()["results"]}
    assert results["LPerm001"]["installed"] is True
    assert results["Other001"]["installed"] is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("server_type", "categories"),
    [("FABRIC", ["categories:fabric"]), ("QUILT", ["categories:quilt", "categories:fabric"]), ("FOLIA", ["categories:folia"])],
)
def test_search_facets_follow_loader(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth, server_type: str, categories: list[str]
) -> None:
    inst = make_server(server_type)
    assert api.get(url(inst, "search")).status_code == 200
    facets = mr.search_params[-1]["facets"]
    assert categories in facets
    assert [f"project_type:{'plugin' if server_type == 'FOLIA' else 'mod'}"] in facets


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["offset=-1", "offset=abc", "limit=500", "sort=bogus", "q=" + "x" * 201])
def test_search_validates_parameters(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth, query: str) -> None:
    resp = api.get(url(make_server("PAPER"), f"search?{query}"))
    assert resp.status_code == 400
    assert mr.search_params == []


@pytest.mark.django_db
def test_project_versions_lists_only_compatible(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    seed_luckperms(mr)
    mr.ignore_version_filters = True  # upstream filter misbehaving must not leak incompatible builds
    inst = make_server("PAPER")
    resp = api.get(url(inst, "projects/luckperms/versions"))
    assert resp.status_code == 200, resp.content
    ids = [v["id"] for v in resp.json()["versions"]]
    assert ids == ["LPbeta01", "LPnew001", "LPold001"]
    assert resp.json()["project"]["id"] == "LPerm001"


@pytest.mark.django_db
def test_project_versions_refuses_mod_project_on_plugin_server(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    mr.project("FabApi01", "fabric-api", "Fabric API", ["fabric", "quilt"])
    resp = api.get(url(make_server("PAPER"), "projects/fabric-api/versions"))
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "incompatible_loader"
    assert "not a plugin" in err["message"]


# ======================================================================================
# Install
# ======================================================================================
@pytest.mark.django_db
def test_install_latest_stable_release(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    seed_luckperms(mr)
    inst = make_server("PAPER")
    resp = api.post(url(inst, "install"), {"project_id": "luckperms"}, format="json")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert [i["version_id"] for i in body["installed"]] == ["LPnew001"]  # newest *release*, not the beta
    fetch = agent.called("fetch_file")
    assert len(fetch) == 1
    assert fetch[0]["path"] == "plugins/luckperms-5.4.9-bukkit.jar"
    assert fetch[0]["url"].startswith("https://cdn.modrinth.com/data/LPerm001/versions/LPnew001/")
    assert fetch[0]["sha1"] == mr.sha1("LPnew001")
    assert fetch[0]["sha512"] == mr.versions["LPnew001"]["files"][0]["hashes"]["sha512"]
    assert fetch[0]["overwrite"] is False
    assert "plugins" in {p for (_iid, p) in agent.dirs}
    event = AuditEvent.objects.get(action="content.install", success=True)
    assert event.instance_id == inst.id
    assert event.details["installed"][0]["version_id"] == "LPnew001"


@pytest.mark.django_db
def test_install_explicit_version(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    seed_luckperms(mr)
    inst = make_server("PAPER")
    resp = api.post(url(inst, "install"), {"project_id": "LPerm001", "version_id": "LPbeta01"}, format="json")
    assert resp.status_code == 201, resp.content
    assert resp.json()["installed"][0]["version_type"] == "beta"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("version_id", "code", "fragment"),
    [
        ("LPfab001", "incompatible_loader", "is a mod for Fabric"),
        ("LPmc1201", "incompatible_minecraft_version", "not 1.20.4"),
    ],
)
def test_install_refuses_incompatible_version(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth, version_id: str, code: str, fragment: str
) -> None:
    seed_luckperms(mr)
    inst = make_server("PAPER")
    resp = api.post(url(inst, "install"), {"project_id": "luckperms", "version_id": version_id}, format="json")
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == code and fragment in err["message"]
    assert agent.called("fetch_file") == []
    failed = AuditEvent.objects.get(action="content.install", success=False)
    assert failed.details["error"] == code


@pytest.mark.django_db
def test_install_refuses_plugin_on_mod_server(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    mr.project("Ess00001", "essentialsx", "EssentialsX", ["paper", "spigot", "bukkit"])
    resp = api.post(url(make_server("FABRIC"), "install"), {"project_id": "essentialsx"}, format="json")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "incompatible_loader"
    assert "server plugin" in resp.json()["error"]["message"]


@pytest.mark.django_db
def test_install_refuses_client_only_and_non_mod_projects(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    mr.project("Sodium01", "sodium", "Sodium", ["fabric"], server_side="unsupported")
    mr.project("Pack0001", "somepack", "Some Pack", ["fabric"], project_type="modpack")
    inst = make_server("FABRIC")
    assert api.post(url(inst, "install"), {"project_id": "sodium"}, format="json").json()["error"]["code"] == "client_only"
    assert api.post(url(inst, "install"), {"project_id": "somepack"}, format="json").json()["error"]["code"] == "not_installable"


@pytest.mark.django_db
def test_install_without_stable_release_requires_explicit_choice(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    mr.project("Beta0001", "betamod", "Beta Mod", ["fabric"])
    mr.version(
        "Bv000001",
        "Beta0001",
        "0.1-beta",
        loaders=["fabric"],
        game_versions=["1.20.4"],
        published="2024-01-01T00:00:00Z",
        version_type="beta",
    )
    resp = api.post(url(make_server("FABRIC"), "install"), {"project_id": "betamod"}, format="json")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "no_stable_release"
    resp = api.post(url(make_server("FORGE"), "install"), {"project_id": "betamod"}, format="json")
    assert resp.json()["error"]["code"] == "incompatible_loader"


@pytest.mark.django_db
def test_install_rejects_non_cdn_download(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    mr.project("Ext00001", "external", "External", ["fabric"])
    mr.version(
        "Ev000001",
        "Ext00001",
        "1.0",
        loaders=["fabric"],
        game_versions=["1.20.4"],
        published="2024-01-01T00:00:00Z",
        url_host="evil.example.com",
    )
    resp = api.post(url(make_server("FABRIC"), "install"), {"project_id": "external"}, format="json")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "no_compatible_version"
    assert agent.called("fetch_file") == []


@pytest.mark.django_db
def test_install_already_installed_and_name_clash(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    seed_luckperms(mr)
    inst = make_server("PAPER")
    agent.put(inst, "plugins/lp-old.jar.disabled", mr.sha1("LPold001"))
    resp = api.post(url(inst, "install"), {"project_id": "luckperms"}, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "already_installed"

    agent.files.clear()
    agent.put(inst, "plugins/luckperms-5.4.9-bukkit.jar.disabled", hashlib.sha1(b"unrelated").hexdigest())
    resp = api.post(url(inst, "install"), {"project_id": "luckperms"}, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "file_exists"
    assert agent.called("fetch_file") == []


def _seed_dependency_chain(mr: FakeModrinth) -> None:
    def dep(pid: str, kind: str = "required", vid: str | None = None) -> dict[str, Any]:
        return {"project_id": pid, "version_id": vid, "file_name": None, "dependency_type": kind}

    for pid, slug in [
        ("Root0001", "root"),
        ("DepA0001", "dep-a"),
        ("DepB0001", "dep-b"),
        ("DepC0001", "dep-c"),
        ("DepD0001", "dep-d"),
        ("Lib00001", "lib"),
        ("Opt00001", "optional"),
    ]:
        mr.project(pid, slug, slug.title(), ["fabric"])
    common = {"loaders": ["fabric"], "game_versions": ["1.20.4"], "published": "2024-02-01T00:00:00Z"}
    mr.version(
        "RootV001",
        "Root0001",
        "1.0",
        deps=[
            dep("DepA0001"),
            dep("Lib00001"),
            dep("Opt00001", "optional"),
            {"project_id": None, "version_id": None, "file_name": "secret.jar", "dependency_type": "required"},
        ],
        **common,
    )
    mr.version("DepAV001", "DepA0001", "1.0", deps=[dep("DepB0001"), dep("Lib00001")], **common)
    mr.version("DepBV001", "DepB0001", "1.0", deps=[dep("DepC0001")], **common)
    mr.version("DepCV001", "DepC0001", "1.0", deps=[dep("DepD0001")], **common)
    mr.version("DepDV001", "DepD0001", "1.0", **common)
    mr.version("LibV0001", "Lib00001", "1.0", **common)
    mr.version("OptV0001", "Opt00001", "1.0", **common)


@pytest.mark.django_db
def test_install_reports_missing_required_dependencies(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    _seed_dependency_chain(mr)
    inst = make_server("FABRIC")
    agent.put(inst, "mods/lib.jar", mr.sha1("LibV0001"))
    resp = api.post(url(inst, "install"), {"project_id": "root"}, format="json")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert [i["filename"] for i in body["installed"]] == ["root-1.0.jar"]
    missing = {(m["project_id"], m["reason"]) for m in body["missing_dependencies"]}
    assert missing == {("DepA0001", "not_installed"), (None, "not_on_modrinth")}
    assert next(m for m in body["missing_dependencies"] if m["project_id"] == "DepA0001")["title"] == "Dep-A"


@pytest.mark.django_db
def test_install_dependencies_bounded_depth_without_duplicates(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    _seed_dependency_chain(mr)
    inst = make_server("FABRIC")
    resp = api.post(url(inst, "install"), {"project_id": "root", "install_dependencies": True}, format="json")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    installed = [i["filename"] for i in body["installed"]]
    # root -> A, lib (depth 1) -> B (2) -> C (3); D would be depth 4
    assert sorted(installed) == sorted(["root-1.0.jar", "dep-a-1.0.jar", "lib-1.0.jar", "dep-b-1.0.jar", "dep-c-1.0.jar"])
    assert len(agent.called("fetch_file")) == 5  # lib is required twice but installed once
    assert installed[0] == "root-1.0.jar"
    assert agent.called("fetch_file")[-1]["path"] == "mods/root-1.0.jar"  # dependencies downloaded first
    reasons = {m["project_id"]: m["reason"] for m in body["missing_dependencies"]}
    assert reasons == {"DepD0001": "dependency_limit", None: "not_on_modrinth"}
    assert "Opt00001" not in {i["project_id"] for i in body["installed"]}
    assert sorted(agent.names(inst, "mods")) == sorted(installed)


@pytest.mark.django_db
def test_install_disabled_dependency_is_reported_not_reinstalled(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    _seed_dependency_chain(mr)
    inst = make_server("FABRIC")
    agent.put(inst, "mods/lib.jar.disabled", mr.sha1("LibV0001"))
    resp = api.post(url(inst, "install"), {"project_id": "dep-c", "install_dependencies": True}, format="json")
    body = resp.json()
    assert [i["project_id"] for i in body["installed"]] == ["DepC0001", "DepD0001"]
    resp = api.post(url(inst, "install"), {"project_id": "dep-a", "install_dependencies": True}, format="json")
    body = resp.json()
    assert {m["project_id"]: m["reason"] for m in body["missing_dependencies"]} == {"Lib00001": "disabled"}
    assert "Lib00001" not in {i["project_id"] for i in body["installed"]}


@pytest.mark.django_db
def test_install_rolls_back_when_a_download_fails(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    _seed_dependency_chain(mr)
    inst = make_server("FABRIC")
    agent.fail_fetch_urls.add(mr.versions["RootV001"]["files"][0]["url"])
    resp = api.post(url(inst, "install"), {"project_id": "root", "install_dependencies": True}, format="json")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "hash_mismatch"
    assert agent.names(inst, "mods") == []  # dependencies fetched before the failure were removed
    assert AuditEvent.objects.filter(action="content.install", success=False).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"project_id": "../../etc"},
        {"project_id": "lp", "version_id": "../x"},
        {"project_id": "lp", "install_dependencies": "yes"},
    ],
)
def test_install_validates_body(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth, body: dict[str, Any]
) -> None:
    resp = api.post(url(make_server("PAPER"), "install"), body, format="json")
    assert resp.status_code == 400
    assert agent.calls == []


@pytest.mark.django_db
def test_install_refused_while_busy_or_unpinned(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    from apps.jobs.models import Job

    seed_luckperms(mr)
    inst = make_server("PAPER")
    job = Job.objects.create(kind="install", title="Install server", instance=inst, status="running")
    inst.active_job = job
    inst.save(update_fields=["active_job"])
    resp = api.post(url(inst, "install"), {"project_id": "luckperms"}, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "instance_busy"

    latest = make_server("PAPER", minecraft_version="LATEST")
    resp = api.post(url(latest, "install"), {"project_id": "luckperms"}, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "minecraft_version_unpinned"
    assert agent.called("fetch_file") == []


# ======================================================================================
# Updates
# ======================================================================================
@pytest.mark.django_db
def test_updates_report_newer_compatible_versions(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    seed_luckperms(mr)
    mr.project("Up000001", "uptodate", "Up To Date", ["paper"])
    mr.version("UpV00001", "Up000001", "2.0", loaders=["paper"], game_versions=["1.20.4"], published="2024-01-01T00:00:00Z")
    inst = make_server("PAPER")
    agent.put(inst, "plugins/LuckPerms-5.4.0.jar", mr.sha1("LPold001"))
    agent.put(inst, "plugins/uptodate.jar", mr.sha1("UpV00001"))
    agent.put(inst, "plugins/manual.jar", hashlib.sha1(b"m").hexdigest())

    resp = api.get(url(inst, "updates"))
    assert resp.status_code == 200, resp.content
    data = resp.json()
    assert data["checked"] == 2 and data["unidentified"] == 1
    assert len(data["updates"]) == 1
    upd = data["updates"][0]
    assert upd["filename"] == "LuckPerms-5.4.0.jar"
    assert upd["current"]["id"] == "LPold001"
    assert upd["latest"]["id"] == "LPnew001"  # stable install is not offered the newer beta
    assert upd["latest"]["version_number"] == "5.4.9-bukkit"
    body = mr.update_bodies[-1]
    assert body["loaders"] == ["paper", "spigot", "bukkit"] and body["game_versions"] == ["1.20.4"]
    assert body["version_types"] == ["release"]


@pytest.mark.django_db
def test_update_downloads_then_removes_old_file(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    seed_luckperms(mr)
    mr.project("Chat0001", "chat", "Chat", ["paper"])
    mr.version("ChatV001", "Chat0001", "1.0", loaders=["paper"], game_versions=["1.20.4"], published="2024-01-01T00:00:00Z")
    mr.version("ChatV002", "Chat0001", "2.0", loaders=["paper"], game_versions=["1.20.4"], published="2024-02-01T00:00:00Z")
    inst = make_server("PAPER")
    agent.put(inst, "plugins/LuckPerms-5.4.0.jar", mr.sha1("LPold001"))
    agent.put(inst, "plugins/chat-1.0.jar.disabled", mr.sha1("ChatV001"))
    resp = api.post(url(inst, "update"), {"files": ["LuckPerms-5.4.0.jar", "chat-1.0.jar.disabled"]}, format="json")
    assert resp.status_code == 200, resp.content
    results = {r["filename"]: r for r in resp.json()["results"]}
    assert results["LuckPerms-5.4.0.jar"]["status"] == "updated"
    assert results["LuckPerms-5.4.0.jar"]["new_filename"] == "luckperms-5.4.9-bukkit.jar"
    assert results["LuckPerms-5.4.0.jar"]["to_version"]["id"] == "LPnew001"
    # a disabled file is updated but stays disabled
    assert results["chat-1.0.jar.disabled"]["new_filename"] == "chat-2.0.jar.disabled"
    assert agent.names(inst, "plugins") == ["chat-2.0.jar.disabled", "luckperms-5.4.9-bukkit.jar"]
    order = [(n, kw.get("path") or kw["args"][1]) for n, kw in agent.calls if n in ("fetch_file", "delete_files")]
    assert order == [
        ("fetch_file", "plugins/luckperms-5.4.9-bukkit.jar"),
        ("delete_files", ["plugins/LuckPerms-5.4.0.jar"]),
        ("fetch_file", "plugins/chat-2.0.jar.disabled"),
        ("delete_files", ["plugins/chat-1.0.jar.disabled"]),
    ]
    assert AuditEvent.objects.filter(action="content.update", success=True).count() == 1


@pytest.mark.django_db
def test_update_refuses_to_create_enabled_and_disabled_twins(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    seed_luckperms(mr)
    inst = make_server("PAPER")
    agent.put(inst, "plugins/LuckPerms-5.4.0.jar", mr.sha1("LPold001"))
    agent.put(inst, "plugins/luckperms-5.4.9-bukkit.jar.disabled", hashlib.sha1(b"other").hexdigest())
    result = api.post(url(inst, "update"), {"files": ["LuckPerms-5.4.0.jar"]}, format="json").json()["results"][0]
    assert result["status"] == "failed" and result["reason"] == "file_exists"
    assert agent.called("fetch_file") == []


@pytest.mark.django_db
def test_update_keeps_old_file_when_download_fails(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    seed_luckperms(mr)
    inst = make_server("PAPER")
    agent.put(inst, "plugins/LuckPerms-5.4.0.jar", mr.sha1("LPold001"))
    agent.put(inst, "plugins/manual.jar", hashlib.sha1(b"m").hexdigest())
    agent.fail_fetch_urls.add(mr.versions["LPnew001"]["files"][0]["url"])
    resp = api.post(url(inst, "update"), {"files": ["LuckPerms-5.4.0.jar", "manual.jar"]}, format="json")
    assert resp.status_code == 200
    data = resp.json()
    results = {r["filename"]: r for r in data["results"]}
    assert results["LuckPerms-5.4.0.jar"]["status"] == "failed"
    assert results["LuckPerms-5.4.0.jar"]["reason"] == "hash_mismatch"
    assert results["manual.jar"]["status"] == "skipped"
    assert data["failed"] == 1 and data["updated"] == 0
    assert agent.called("delete_files") == []
    assert "LuckPerms-5.4.0.jar" in agent.names(inst, "plugins")
    assert AuditEvent.objects.filter(action="content.update", success=False).count() == 1


@pytest.mark.django_db
def test_update_same_filename_overwrites_atomically(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth
) -> None:
    mr.project("Same0001", "same", "Same", ["fabric"])
    mr.version(
        "SameV001",
        "Same0001",
        "1",
        loaders=["fabric"],
        game_versions=["1.20.4"],
        published="2024-01-01T00:00:00Z",
        filename="same.jar",
    )
    mr.version(
        "SameV002",
        "Same0001",
        "2",
        loaders=["fabric"],
        game_versions=["1.20.4"],
        published="2024-02-01T00:00:00Z",
        filename="same.jar",
    )
    inst = make_server("FABRIC")
    agent.put(inst, "mods/same.jar", mr.sha1("SameV001"))
    resp = api.post(url(inst, "update"), {"files": ["same.jar"]}, format="json")
    assert resp.json()["results"][0]["status"] == "updated"
    assert agent.called("fetch_file")[0]["overwrite"] is True
    assert agent.called("delete_files") == []
    assert agent.files[(str(inst.id), "mods/same.jar")]["sha1"] == mr.sha1("SameV002")


@pytest.mark.django_db
@pytest.mark.parametrize("files", [None, [], ["../x.jar"], ["missing.jar"], "x.jar"])
def test_update_validates_files(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth, files: Any) -> None:
    resp = api.post(url(make_server("PAPER"), "update"), {"files": files}, format="json")
    assert resp.status_code in (400, 404)
    assert agent.called("fetch_file") == []


# ======================================================================================
# Toggle / delete / upload
# ======================================================================================
@pytest.mark.django_db
def test_toggle_renames_between_jar_and_disabled(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    inst = make_server("FABRIC")
    agent.put(inst, "mods/a.jar", hashlib.sha1(b"a").hexdigest())
    resp = api.post(url(inst, "toggle"), {"filename": "a.jar", "enabled": False}, format="json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["filename"] == "a.jar.disabled" and resp.json()["changed"] is True
    assert agent.called("rename")[0]["args"][1:] == ("mods/a.jar", "mods/a.jar.disabled")
    assert agent.names(inst, "mods") == ["a.jar.disabled"]

    resp = api.post(url(inst, "toggle"), {"filename": "a.jar.disabled", "enabled": True}, format="json")
    assert resp.json()["filename"] == "a.jar"
    assert agent.names(inst, "mods") == ["a.jar"]

    resp = api.post(url(inst, "toggle"), {"filename": "a.jar", "enabled": True}, format="json")
    assert resp.status_code == 200 and resp.json()["changed"] is False
    assert len(agent.called("rename")) == 2
    assert AuditEvent.objects.filter(action="content.toggle", success=True).count() == 2


@pytest.mark.django_db
def test_toggle_conflict_and_validation(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    inst = make_server("FABRIC")
    agent.put(inst, "mods/a.jar", hashlib.sha1(b"a").hexdigest())
    agent.put(inst, "mods/a.jar.disabled", hashlib.sha1(b"b").hexdigest())
    resp = api.post(url(inst, "toggle"), {"filename": "a.jar", "enabled": False}, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "file_exists"
    for body in ({"filename": "../a.jar", "enabled": False}, {"filename": "a.jar", "enabled": "no"}, {"filename": "a.jar"}):
        assert api.post(url(inst, "toggle"), body, format="json").status_code == 400
    resp = api.post(url(inst, "toggle"), {"filename": "gone.jar", "enabled": False}, format="json")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "file_not_found"


@pytest.mark.django_db
def test_delete_file(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    inst = make_server("PAPER")
    agent.put(inst, "plugins/Some Plugin.jar", hashlib.sha1(b"a").hexdigest())
    resp = api.delete(url(inst, "files?filename=" + quote("Some Plugin.jar")))
    assert resp.status_code == 200, resp.content
    assert agent.called("delete_files")[0]["args"][1] == ["plugins/Some Plugin.jar"]
    assert agent.names(inst, "plugins") == []
    assert AuditEvent.objects.get(action="content.delete").details == {"filename": "Some Plugin.jar"}


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["", "../server.jar", "..%2Fserver.jar", "sub/x.jar", "server.properties", "x.jar%00"])
def test_delete_rejects_unsafe_names(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth, name: str) -> None:
    resp = api.delete(url(make_server("PAPER"), f"files?filename={name}"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_filename"
    assert agent.calls == []


@pytest.mark.django_db
def test_upload_plugin_jar(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    inst = make_server("PAPER")
    jar = make_jar({"plugin.yml": b"name: Test\nmain: a.B\nversion: 1\n", "a/B.class": b"\xca\xfe"})
    resp = api.post(url(inst, "upload"), {"file": SimpleUploadedFile("Test.jar", jar)}, format="multipart")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["uploaded"] == {"filename": "Test.jar", "size": len(jar), "sha1": hashlib.sha1(jar).hexdigest()}
    assert body["detected_platforms"] == ["bukkit"]
    assert body["warnings"] == []
    assert "untrusted" in body["notice"]
    up = agent.called("upload")[0]
    assert up["args"][1] == "plugins/Test.jar" and up["overwrite"] is False
    assert agent.files[(str(inst.id), "plugins/Test.jar")]["sha1"] == hashlib.sha1(jar).hexdigest()
    event = AuditEvent.objects.get(action="content.upload", success=True)
    assert event.details["sha1"] == hashlib.sha1(jar).hexdigest()

    # same name again: refused unless overwrite=true
    resp = api.post(url(inst, "upload"), {"file": SimpleUploadedFile("Test.jar", jar)}, format="multipart")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "file_exists"
    resp = api.post(url(inst, "upload") + "?overwrite=true", {"file": SimpleUploadedFile("Test.jar", jar)}, format="multipart")
    assert resp.status_code == 201
    assert agent.called("upload")[-1]["overwrite"] is True


@pytest.mark.django_db
def test_upload_warns_about_wrong_ecosystem(api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    fabric_jar = make_jar({"fabric.mod.json": b"{}"})
    resp = api.post(url(make_server("PAPER"), "upload"), {"file": SimpleUploadedFile("mod.jar", fabric_jar)}, format="multipart")
    assert resp.status_code == 201
    assert any("Fabric mod" in w and "will not load" in w for w in resp.json()["warnings"])

    plugin_jar = make_jar({"plugin.yml": b"name: P\nmain: a.B\n"})
    resp = api.post(url(make_server("FOLIA"), "upload"), {"file": SimpleUploadedFile("p.jar", plugin_jar)}, format="multipart")
    assert any("Folia" in w for w in resp.json()["warnings"])
    folia_jar = make_jar({"plugin.yml": b"name: P\nmain: a.B\nfolia-supported: true\n"})
    resp = api.post(url(make_server("FOLIA"), "upload"), {"file": SimpleUploadedFile("f.jar", folia_jar)}, format="multipart")
    assert resp.json()["warnings"] == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "content", "code"),
    [
        ("evil.sh", b"#!/bin/sh", "invalid_filename"),
        ("x.jar.disabled", make_jar({"plugin.yml": b"a"}), "invalid_filename"),
        ("broken.jar", b"definitely not a zip", "invalid_jar"),
    ],
)
def test_upload_validation(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth, name: str, content: bytes, code: str
) -> None:
    resp = api.post(url(make_server("PAPER"), "upload"), {"file": SimpleUploadedFile(name, content)}, format="multipart")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == code
    assert agent.called("upload") == []


@pytest.mark.django_db
def test_upload_size_limit(
    api: APIClient, make_server: Any, agent: ContentAgent, mr: FakeModrinth, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 100)
    jar = make_jar({"plugin.yml": b"x" * 500})
    resp = api.post(url(make_server("PAPER"), "upload"), {"file": SimpleUploadedFile("big.jar", jar)}, format="multipart")
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "file_too_large"
    assert agent.called("upload") == []


@pytest.mark.django_db
def test_upload_oversized_body_refused_before_parsing_with_session_auth(
    make_server: Any, agent: ContentAgent, mr: FakeModrinth, owner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real session login runs DRF's CSRF check, which reads request.POST (parsing and
    spooling the whole multipart body); the size limit must be enforced before that."""
    from django.conf import settings
    from django.middleware.csrf import _get_new_csrf_string
    from rest_framework.parsers import MultiPartParser

    from apps.content import views

    parsed: list[int] = []
    original = MultiPartParser.parse

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        parsed.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MultiPartParser, "parse", spy)
    monkeypatch.setattr(views, "UPLOAD_OVERHEAD", 0)
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 1000)
    inst = make_server("PAPER")
    client = APIClient(enforce_csrf_checks=True)
    assert client.login(username=owner.username, password="x-Correct-Horse-1")
    token = _get_new_csrf_string()
    client.cookies[settings.CSRF_COOKIE_NAME] = token
    jar = make_jar({"plugin.yml": b"x" * 5000})
    resp = client.post(
        url(inst, "upload"), {"file": SimpleUploadedFile("big.jar", jar)}, format="multipart", HTTP_X_CSRFTOKEN=token
    )
    assert resp.status_code == 413, resp.content
    assert resp.json()["error"]["code"] == "file_too_large"
    assert parsed == []
    assert agent.called("upload") == []

    # within the limit the same authenticated flow works (CSRF passes, body is parsed once)
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
    small = make_jar({"plugin.yml": b"name: P\nmain: a.B\n"})
    resp = client.post(
        url(inst, "upload"), {"file": SimpleUploadedFile("p.jar", small)}, format="multipart", HTTP_X_CSRFTOKEN=token
    )
    assert resp.status_code == 201, resp.content
    assert len(parsed) == 1


# ======================================================================================
# Permissions
# ======================================================================================
@pytest.mark.django_db
def test_permissions(make_server: Any, agent: ContentAgent, mr: FakeModrinth) -> None:
    from apps.accounts.models import User

    seed_luckperms(mr)
    inst = make_server("PAPER")
    agent.put(inst, "plugins/a.jar", hashlib.sha1(b"a").hexdigest())
    viewer = granted_client(inst, ["instance.view"], "ct-viewer")
    manager = granted_client(inst, ["instance.view", "instance.content"], "ct-manager")
    stranger = APIClient()
    stranger.force_authenticate(User.objects.create_user(username="ct-stranger", password="x-Correct-Horse-1"))

    assert viewer.get(url(inst)).status_code == 200
    assert viewer.get(url(inst, "updates")).status_code == 200
    mutations = [
        ("post", "install", {"project_id": "luckperms"}),
        ("post", "update", {"files": ["a.jar"]}),
        ("post", "toggle", {"filename": "a.jar", "enabled": False}),
    ]
    for method, suffix, body in mutations:
        assert getattr(viewer, method)(url(inst, suffix), body, format="json").status_code == 403
    assert viewer.delete(url(inst, "files?filename=a.jar")).status_code == 403
    jar = make_jar({"plugin.yml": b"name: x"})
    assert viewer.post(url(inst, "upload"), {"file": SimpleUploadedFile("x.jar", jar)}, format="multipart").status_code == 403
    assert stranger.get(url(inst)).status_code == 404
    assert stranger.post(url(inst, "install"), {"project_id": "luckperms"}, format="json").status_code == 404
    assert agent.called("fetch_file") == [] and agent.called("rename") == []

    assert manager.post(url(inst, "toggle"), {"filename": "a.jar", "enabled": False}, format="json").status_code == 200
    assert manager.post(url(inst, "install"), {"project_id": "luckperms"}, format="json").status_code == 201
    assert APIClient().get(url(inst)).status_code in (401, 403)
