import io
import json
import zipfile

import httpx
import pytest
import respx

from apps.modpacks.mrpack import inspect_mrpack

URL = "https://cdn.modrinth.com/data/abc/versions/def/pack.mrpack"


def build_pack(with_checker: bool) -> bytes:
    buf = io.BytesIO()
    index = {"formatVersion": 1, "game": "minecraft", "name": "Test Pack",
             "dependencies": {"minecraft": "1.20.1", "forge": "47.4.20"},
             "files": [{"path": "mods/a.jar", "env": {"client": "required", "server": "required"}},
                       {"path": "mods/b.jar", "env": {"client": "required", "server": "unsupported"}}]}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("modrinth.index.json", json.dumps(index))
        zf.writestr("overrides/mods/extra.jar", b"\x00" * 1000)
        zf.writestr("overrides/config/big.toml", "x" * 300_000)  # pushes the central directory past the tail window? no
        if with_checker:
            zf.writestr("overrides/config/missing_mods_checker.json",
                        json.dumps([{"displayName": "FallingTree", "pattern": "FallingTree.jar",
                                     "url": "https://www.curseforge.com/x/download/1", "destination": "mods"}]))
    return buf.getvalue()


def range_handler(data: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("range", "")
        assert rng.startswith("bytes=") and "-" in rng and not rng.startswith("bytes=-")
        start, end = (int(x) for x in rng[6:].split("-"))
        end = min(end, len(data) - 1)
        return httpx.Response(206, content=data[start:end + 1],
                              headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"})
    return handler


@pytest.mark.parametrize("with_checker", [True, False])
def test_inspect_mrpack(with_checker, settings):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}
    data = build_pack(with_checker)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(URL).mock(side_effect=range_handler(data))
        r = inspect_mrpack(URL, None)
    assert r["available"] is True
    assert (r["minecraft_version"], r["loader"], r["loader_version"]) == ("1.20.1", "forge", "47.4.20")
    assert r["files_total"] == 2 and r["files_server_unsupported"] == 1 and r["override_mods"] == 1
    assert len(r["manual_downloads"]) == (1 if with_checker else 0)
    assert bool(r["warnings"]) is with_checker


def test_inspect_rejects_non_modrinth_hosts():
    assert inspect_mrpack("https://evil.example/pack.mrpack")["available"] is False
    assert inspect_mrpack("http://cdn.modrinth.com/pack.mrpack")["available"] is False
