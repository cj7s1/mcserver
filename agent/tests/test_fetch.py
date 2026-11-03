"""fetch.fetch_into: only HTTPS on allow-listed provider CDNs, hashes required and verified."""

from __future__ import annotations

import hashlib
import io

import pytest

from mcagent import fetch
from mcagent.docker_ops import AgentError
from mcagent.safefs import PathError, SafeRoot

DATA = b"PK\x03\x04 pretend this is a mod jar"
SHA1 = hashlib.sha1(DATA).hexdigest()
SHA512 = hashlib.sha512(DATA).hexdigest()
URL = "https://cdn.modrinth.com/data/AANobbMI/versions/abc/sodium.jar"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_FETCH_ALLOWED_HOSTS", raising=False)
    inst = tmp_path / "inst"
    (inst / "mods").mkdir(parents=True)
    return SafeRoot(inst), inst


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, final_url: str):
        super().__init__(data)
        self._url = final_url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def served(monkeypatch):
    """Patch the opener; records requests. Set .data / .final_url / .error to change behaviour."""

    class Server:
        data = DATA
        final_url = None
        error: Exception | None = None
        requests: list = []

    def opener(req, timeout=None):
        Server.requests.append(req)
        if Server.error:
            raise Server.error
        return FakeResponse(Server.data, Server.final_url or req.full_url)

    Server.requests = []
    monkeypatch.setattr(fetch, "_open", opener)
    return Server


def leftovers(inst):
    return sorted(p.name for p in (inst / "mods").iterdir())


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.modrinth.com/data/x.jar",  # not HTTPS
        "ftp://cdn.modrinth.com/x.jar",
        "file:///etc/passwd",
        "https://evil.example.com/x.jar",
        "https://cdn.modrinth.com.evil.example.com/x.jar",
        "https://cdn.modrinth.com@evil.example.com/x.jar",  # userinfo trick
        "https://169.254.169.254/latest/meta-data/",
        "https://localhost:8700/v1/info",
        "",
    ],
)
def test_non_allow_listed_urls_rejected(root, served, url):
    sr, inst = root
    with pytest.raises(AgentError) as exc:
        fetch.fetch_into(sr, {"url": url, "path": "mods/x.jar", "sha1": SHA1})
    assert exc.value.code == "url_not_allowed" and exc.value.status == 403
    assert served.requests == []
    assert leftovers(inst) == []


def test_hash_required(root, served):
    sr, _ = root
    with pytest.raises(AgentError) as exc:
        fetch.fetch_into(sr, {"url": URL, "path": "mods/x.jar"})
    assert exc.value.code == "hash_required"
    with pytest.raises(AgentError):
        fetch.fetch_into(sr, {"url": URL, "path": "mods/x.jar", "sha1": "", "sha512": None})
    assert served.requests == []


def test_path_required(root, served):
    sr, _ = root
    with pytest.raises(AgentError) as exc:
        fetch.fetch_into(sr, {"url": URL, "path": "", "sha1": SHA1})
    assert exc.value.code == "invalid_path"


def test_successful_download_is_verified_and_written(root, served):
    sr, inst = root
    result = fetch.fetch_into(
        sr, {"url": URL, "path": "mods/sodium.jar", "sha1": SHA1.upper(), "sha512": SHA512}
    )
    assert result == {"path": "mods/sodium.jar", "size": len(DATA), "sha1": SHA1, "sha512": SHA512}
    assert (inst / "mods" / "sodium.jar").read_bytes() == DATA
    assert leftovers(inst) == ["sodium.jar"]  # no temp files left behind
    assert served.requests[0].get_header("User-agent").startswith("mcpanel-agent/")


@pytest.mark.parametrize(
    "hashes", [{"sha1": "0" * 40}, {"sha512": "0" * 128}, {"sha1": SHA1, "sha512": "0" * 128}]
)
def test_hash_mismatch_leaves_nothing_behind(root, served, hashes):
    sr, inst = root
    with pytest.raises(AgentError) as exc:
        fetch.fetch_into(sr, {"url": URL, "path": "mods/sodium.jar", **hashes})
    assert exc.value.code == "hash_mismatch"
    assert leftovers(inst) == []


def test_redirect_to_unlisted_host_rejected(root, served):
    sr, inst = root
    served.final_url = "https://evil.example.com/payload.jar"
    with pytest.raises(AgentError) as exc:
        fetch.fetch_into(sr, {"url": URL, "path": "mods/sodium.jar", "sha1": SHA1})
    assert exc.value.code == "url_not_allowed"
    assert leftovers(inst) == []


def test_network_errors_are_reported(root, served):
    sr, inst = root
    served.error = OSError("connection reset")
    with pytest.raises(AgentError) as exc:
        fetch.fetch_into(sr, {"url": URL, "path": "mods/sodium.jar", "sha1": SHA1})
    assert exc.value.code == "download_failed"
    assert leftovers(inst) == []


def test_existing_file_requires_overwrite(root, served):
    sr, inst = root
    (inst / "mods" / "sodium.jar").write_bytes(b"old")
    with pytest.raises(PathError) as exc:
        fetch.fetch_into(sr, {"url": URL, "path": "mods/sodium.jar", "sha1": SHA1})
    assert exc.value.code == "exists"
    assert served.requests == []
    fetch.fetch_into(sr, {"url": URL, "path": "mods/sodium.jar", "sha1": SHA1, "overwrite": True})
    assert (inst / "mods" / "sodium.jar").read_bytes() == DATA


def test_destination_is_confined(root, served, tmp_path):
    sr, _ = root
    with pytest.raises(PathError):
        fetch.fetch_into(sr, {"url": URL, "path": "../outside.jar", "sha1": SHA1})
    assert not (tmp_path / "outside.jar").exists()


def test_extra_hosts_via_env(root, served, monkeypatch):
    sr, inst = root
    monkeypatch.setenv("AGENT_FETCH_ALLOWED_HOSTS", " Mirror.Example.org , ")
    assert "mirror.example.org" in fetch.allowed_hosts()
    fetch.fetch_into(sr, {"url": "https://MIRROR.example.org/x.jar", "path": "mods/x.jar", "sha1": SHA1})
    assert (inst / "mods" / "x.jar").read_bytes() == DATA


class _Fp:
    closed = False

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example.com/payload.jar",
        "http://cdn.modrinth.com/data/x.jar",  # downgrade to plain HTTP
        "https://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8700/v1/info",
    ],
)
def test_redirect_hops_are_checked_before_they_are_followed(target):
    import urllib.request

    handler = fetch._AllowListRedirects()
    req = urllib.request.Request(URL)
    fp = _Fp()
    with pytest.raises(AgentError) as exc:
        handler.redirect_request(req, fp, 302, "Found", {}, target)
    assert exc.value.code == "url_not_allowed"
    assert fp.closed


def test_redirect_between_allow_listed_hosts_is_followed():
    import urllib.request

    handler = fetch._AllowListRedirects()
    req = urllib.request.Request("https://edge.forgecdn.net/files/1/2/x.jar")
    new = handler.redirect_request(
        req, _Fp(), 302, "Found", {}, "https://mediafilez.forgecdn.net/files/1/2/x.jar"
    )
    assert new.full_url == "https://mediafilez.forgecdn.net/files/1/2/x.jar"


def test_real_opener_uses_the_checking_redirect_handler(monkeypatch):
    import urllib.request

    seen = {}

    class Opener:
        def open(self, req, timeout=None):
            raise OSError("not contacting the network in tests")

    def build_opener(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    with pytest.raises(OSError):
        fetch._open(urllib.request.Request(URL), timeout=1)
    assert any(isinstance(h, fetch._AllowListRedirects) for h in seen["handlers"])
