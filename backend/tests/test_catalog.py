"""catalog.providers: Java resolution from Mojang metadata, version ordering, NeoForge mapping.

Upstream HTTP is mocked with respx using the response shapes documented in
docs/reference/version-metadata.md.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from apps.catalog import providers
from apps.core.errors import UpstreamError

JAVA = {
    "1.12.2": 8,
    "1.16.5": 8,
    "1.17.1": 16,
    "1.18.2": 17,
    "1.20.4": 17,
    "1.20.5": 21,
    "1.21.11": 21,
    "26.1": 25,
    "26.3": 25,
    "1.99-hypothetical": 18,
    "1.98-future": 30,
}


def version_url(v: str) -> str:
    return f"https://piston-meta.mojang.com/v1/packages/0123abcd/{v}.json"


def manifest(latest: str = "26.3", versions: dict[str, int] = JAVA) -> dict:
    return {
        "latest": {"release": latest, "snapshot": "26.4-snapshot-7"},
        "versions": [
            {"id": v, "type": "release", "url": version_url(v), "releaseTime": "2026-01-01T00:00:00+00:00"} for v in versions
        ],
    }


@pytest.fixture
def mojang():
    with respx.mock(assert_all_called=False) as router:
        router.get(providers.MOJANG_MANIFEST, name="manifest").mock(return_value=httpx.Response(200, json=manifest()))
        for v, major in JAVA.items():
            router.get(version_url(v), name=v).mock(
                return_value=httpx.Response(200, json={"id": v, "javaVersion": {"component": "x", "majorVersion": major}})
            )
        yield router


@pytest.mark.parametrize(
    ("mc", "server_type", "required", "java"),
    [
        ("1.20.4", "VANILLA", 17, 17),
        ("1.20.5", "PAPER", 21, 21),
        ("1.21.11", "FABRIC", 21, 21),
        ("26.1", "VANILLA", 25, 25),
        ("1.17.1", "VANILLA", 16, 16),
        ("1.17.1", "FORGE", 16, 16),  # Forge 1.17 cannot run on Java 8
        ("1.16.5", "VANILLA", 8, 8),
        ("1.18.2", "FORGE", 17, 17),
        ("1.99-hypothetical", "VANILLA", 18, 21),  # smallest published tag >= requirement
        ("1.98-future", "VANILLA", 30, 25),  # newer than any tag: newest tag
    ],
)
def test_resolve_java_from_mojang(mojang, mc, server_type, required, java):
    info = providers.resolve_java(mc, server_type)
    assert info == {"minecraft_version": mc, "required": required, "java_version": java, "reason": "Mojang metadata"}


@pytest.mark.parametrize("mc", ["1.12.2", "1.16.5"])
def test_forge_1_16_5_and_older_forced_to_java8(mojang, mc):
    info = providers.resolve_java(mc, "FORGE")
    assert info["java_version"] == 8
    assert "Java 8" in info["reason"]


def test_unknown_versions_assume_modern_java(mojang):
    assert providers.resolve_java("1.22.9", "VANILLA")["java_version"] == 21
    unknown = providers.resolve_java("27.1", "VANILLA")
    assert unknown["java_version"] == 25
    assert unknown["reason"].startswith("Unknown version")


def test_latest_resolves_through_manifest_and_is_not_cached_as_latest(mojang):
    assert providers.resolve_java("LATEST", "VANILLA")["java_version"] == 25
    # A new release with another Java requirement appears: LATEST must follow it once the
    # (5 minute) manifest cache expires instead of staying pinned to the old answer.
    from django.core.cache import cache

    cache.delete("catalog:mojang:manifest")
    mojang["manifest"].mock(return_value=httpx.Response(200, json=manifest(latest="1.20.4")))
    assert providers.resolve_java("LATEST", "VANILLA")["java_version"] == 17


def test_java_requirement_is_cached(mojang):
    assert providers.java_requirement("1.20.4") == 17
    assert providers.java_requirement("1.20.4") == 17
    assert mojang["manifest"].call_count == 1
    assert mojang["1.20.4"].call_count == 1
    assert sum(mojang[v].call_count for v in JAVA) == 1


def test_version_json_404_is_unknown(mojang):
    mojang["1.20.4"].mock(return_value=httpx.Response(404))
    assert providers.java_requirement("1.20.4") is None


def test_upstream_errors_are_reported():
    with respx.mock() as router:
        router.get(providers.MOJANG_MANIFEST).mock(return_value=httpx.Response(503))
        with pytest.raises(UpstreamError) as exc:
            providers.resolve_java("1.20.4", "VANILLA")
        assert exc.value.error_code == "upstream_error"
        assert exc.value.status_code == 502
    with respx.mock() as router:
        router.get(providers.MOJANG_MANIFEST).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(UpstreamError) as exc:
            providers.mojang_manifest()
        assert exc.value.error_code == "upstream_unreachable"


def test_requests_send_the_panel_user_agent(mojang, settings):
    providers.mojang_manifest()
    request = mojang["manifest"].calls.last.request
    assert request.headers["User-Agent"] == settings.MCPANEL_USER_AGENT


# --- version_key -------------------------------------------------------------------------
def test_version_key_orders_minecraft_versions():
    shuffled = ["1.20.10", "26.1", "1.9", "1.20", "1.21-rc1", "1.20.1", "26.1-snapshot-1", "1.21", "1.20.2", "26.1.2"]
    assert sorted(shuffled, key=providers.version_key) == [
        "1.9",
        "1.20",
        "1.20.1",
        "1.20.2",
        "1.20.10",
        "1.21-rc1",
        "1.21",
        "26.1-snapshot-1",
        "26.1",
        "26.1.2",
    ]


def test_version_key_orders_loader_versions():
    assert sorted(["0.16.9", "0.16.14", "0.15.11"], key=providers.version_key)[-1] == "0.16.14"
    assert sorted(["47.4.3", "47.4.20", "47.10.0"], key=providers.version_key) == ["47.4.3", "47.4.20", "47.10.0"]
    assert providers.version_key("21.1.0-beta") < providers.version_key("21.1.0")
    assert providers.version_key("0.30.2") < providers.version_key("0.31.0-beta.5")


def test_is_at_most():
    assert providers.is_at_most("1.16.5", "1.16.5")
    assert providers.is_at_most("1.12.2", "1.16.5")
    assert providers.is_at_most("1.16", "1.16.5")
    assert not providers.is_at_most("1.17", "1.16.5")
    assert not providers.is_at_most("26.1", "1.16.5")


# --- NeoForge version -> Minecraft version -----------------------------------------------
@pytest.mark.parametrize(
    ("neo", "mc"),
    [
        ("20.2.3-beta", "1.20.2"),
        ("20.4.237", "1.20.4"),
        ("20.6.119", "1.20.6"),
        ("21.0.167", "1.21"),
        ("21.1.265", "1.21.1"),
        ("21.11.48", "1.21.11"),
        ("26.1.0.19-beta", "26.1"),
        ("26.1.1.15-beta", "26.1.1"),
        ("26.1.2.126", "26.1.2"),
        ("26.2.0.93", "26.2"),
        ("26.3.0.101", "26.3"),
        ("26.1.0.0-alpha.14+snapshot-11", "26.1"),
        ("0.25w14craftmine.3-beta", None),
        ("47.1.82", None),  # stray legacy 1.20.1 forge-artifact build
        ("19.0.1", None),
        ("not.a.version", None),
    ],
)
def test_neoforge_mc_mapping(neo, mc):
    assert providers._neoforge_mc(neo) == mc


def test_neoforge_game_versions_from_maven_listing():
    listing = {
        "isSnapshot": False,
        "versions": [
            "0.25w14craftmine.3-beta",
            "20.2.3-beta",
            "20.4.237",
            "21.1.264",
            "21.1.265",
            "26.1.0.0-alpha.14+snapshot-11",
        ],
    }
    with respx.mock() as router:
        router.get(providers.NEOFORGE_VERSIONS).mock(return_value=httpx.Response(200, json=listing))
        versions = providers.neoforge_game_versions()
        loaders = providers.neoforge_loader_versions("1.21.1")
    assert versions == [
        {"version": "1.21.1", "stable": True},
        {"version": "1.20.4", "stable": True},
        {"version": "1.20.2", "stable": False},
        {"version": "1.20.1", "stable": True},  # legacy net.neoforged:forge artifact
    ]
    assert [v["version"] for v in loaders] == ["21.1.265", "21.1.264"]
