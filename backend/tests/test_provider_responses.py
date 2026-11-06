"""Providers must turn empty or malformed upstream bodies into clean API errors, never HTTP 500."""

from __future__ import annotations

import httpx
import pytest
import respx

from apps.core.errors import UpstreamError
from apps.modpacks import providers


@pytest.fixture
def cf_key(monkeypatch):
    monkeypatch.setattr(providers, "curseforge_key", lambda: "test-key")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b""),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"error": "maintenance"}),
    ],
)
def test_curseforge_empty_or_unwrapped_response_is_an_upstream_error(cf_key, response):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=providers.CURSEFORGE_API).mock(return_value=response)
        with pytest.raises(UpstreamError) as err:
            providers.CurseForge()._get("/v1/mods/123")
    assert err.value.error_code == "provider_bad_response"


def test_curseforge_wrapped_response_passes_through(cf_key):
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=providers.CURSEFORGE_API).mock(return_value=httpx.Response(200, json={"data": {"id": 1}}))
        assert providers.CurseForge()._get("/v1/mods/1") == {"data": {"id": 1}}


def test_non_json_body_is_an_upstream_error():
    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.modrinth.com/v2/project/x").mock(
            return_value=httpx.Response(200, content=b"<html>Bad gateway</html>", headers={"content-type": "text/html"})
        )
        with pytest.raises(UpstreamError) as err:
            providers._request("GET", "https://api.modrinth.com/v2/project/x", provider="Modrinth")
    assert err.value.error_code == "provider_bad_response"
