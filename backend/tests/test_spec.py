"""instances.spec: itzg environment + agent container spec generation."""

from __future__ import annotations

import pytest

from apps.core.models import SystemSetting
from apps.instances.models import Instance, ServerType
from apps.instances.spec import (
    LOADER_ENV,
    MANAGED_ENV,
    build_env,
    build_spec,
    current_spec_hash,
    image_for,
    restart_required,
    spec_hash,
    spec_outdated,
)

pytestmark = pytest.mark.django_db


def inst(**fields) -> Instance:
    values = {
        "name": "Test",
        "game_port": 25600,
        "server_type": ServerType.VANILLA,
        "minecraft_version": "1.20.4",
        "java_version": 17,
        "image": "itzg/minecraft-server:stable-java17",
        "memory_mb": 2048,
        "jvm_flags_preset": "none",
        "stop_timeout": 90,
    }
    values.update(fields)
    return Instance(**values)


def test_core_env_for_vanilla():
    env = build_env(inst())
    assert env["EULA"] == "TRUE"
    assert env["TYPE"] == "VANILLA"
    assert env["VERSION"] == "1.20.4"
    assert env["MEMORY"] == "2048M"
    assert env["STOP_DURATION"] == "70"
    assert env["TZ"] == "UTC"
    assert not set(env) & set(LOADER_ENV.values())
    assert "JVM_OPTS" not in env and "USE_AIKAR_FLAGS" not in env


def test_blank_version_means_latest():
    assert build_env(inst(minecraft_version=""))["VERSION"] == "LATEST"


@pytest.mark.parametrize(("timeout", "expected"), [(90, "70"), (25, "10"), (15, "10"), (900, "880")])
def test_stop_duration_leaves_headroom_for_docker_stop(timeout, expected):
    assert build_env(inst(stop_timeout=timeout))["STOP_DURATION"] == expected


def test_managed_env_keys_cannot_be_overridden():
    overrides = {
        "EULA": "FALSE",
        "type": "PAPER",
        "Memory": "64G",
        "MAX_MEMORY": "64G",
        "JVM_OPTS": "-Xmx64G",
        "RCON_PASSWORD": "hunter2",
        "RCON_PASSWORD_FILE": "/etc/shadow",
        "SERVER_PORT": "1",
        "UID": "0",
        "STOP_DURATION": "1",
        "CF_API_KEY": "stolen",
        "MOTD": "custom motd",
        "DIFFICULTY": "hard",
    }
    env = build_env(inst(env=overrides))
    assert env["EULA"] == "TRUE"
    assert env["TYPE"] == "VANILLA"
    assert env["MEMORY"] == "2048M"
    assert env["STOP_DURATION"] == "70"
    for key in (
        "MAX_MEMORY",
        "JVM_OPTS",
        "RCON_PASSWORD",
        "RCON_PASSWORD_FILE",
        "SERVER_PORT",
        "UID",
        "CF_API_KEY",
        "type",
        "Memory",
    ):
        assert key not in env
    # non-managed advanced variables pass through
    assert env["MOTD"] == "custom motd"
    assert env["DIFFICULTY"] == "hard"


def test_every_managed_key_is_dropped_case_insensitively():
    env = build_env(inst(env={k.lower(): "x" for k in MANAGED_ENV}))
    assert not {k for k in env if k.islower()}


def test_user_tz_is_respected_and_system_tz_used_otherwise():
    assert build_env(inst(env={"TZ": "Europe/Berlin"}))["TZ"] == "Europe/Berlin"
    SystemSetting.set("timezone", "America/New_York")
    assert build_env(inst())["TZ"] == "America/New_York"


def test_jvm_args_and_flag_presets():
    env = build_env(inst(jvm_args="  -XX:+UseG1GC \n  -Dfoo=bar ", jvm_flags_preset="aikar"))
    assert env["JVM_OPTS"] == "-XX:+UseG1GC -Dfoo=bar"
    assert env["USE_AIKAR_FLAGS"] == "true"
    assert build_env(inst(jvm_flags_preset="meowice", java_version=21))["USE_MEOWICE_FLAGS"] == "true"
    # MeowIce flags need Java 17+
    assert "USE_MEOWICE_FLAGS" not in build_env(inst(jvm_flags_preset="meowice", java_version=8))
    assert "USE_AIKAR_FLAGS" not in build_env(inst(jvm_flags_preset="none"))


@pytest.mark.parametrize(("server_type", "key"), sorted(LOADER_ENV.items()))
def test_loader_version_keys(server_type, key):
    env = build_env(inst(server_type=server_type, minecraft_version="1.21.1", loader_version="1.2.3"))
    assert env["TYPE"] == server_type
    assert env["VERSION"] == "1.21.1"
    assert env[key] == "1.2.3"
    assert not (set(LOADER_ENV.values()) - {key}) & set(env)
    # blank loader version => let the image pick the recommended/latest build
    assert key not in build_env(inst(server_type=server_type, loader_version=""))


def test_modrinth_modpack_env():
    env = build_env(
        inst(
            server_type=ServerType.MODRINTH,
            minecraft_version="1.20.1",
            modpack_project_id="1KVo5zza",
            modpack_slug="fabulously-optimized",
            modpack_version_id="abcd1234",
            modpack_loader="fabric",
            loader_version="0.16.14",
        )
    )
    assert env["TYPE"] == "MODRINTH"
    assert env["MODRINTH_MODPACK"] == "1KVo5zza"
    assert env["MODRINTH_VERSION"] == "abcd1234"
    assert env["VERSION"] == "1.20.1"
    assert env["MODRINTH_LOADER"] == "fabric"
    assert "FABRIC_LOADER_VERSION" not in env  # the pack pins its loader


def test_modrinth_falls_back_to_slug_and_skips_latest_version():
    env = build_env(
        inst(server_type=ServerType.MODRINTH, minecraft_version="LATEST", modpack_slug="cobblemon", modpack_loader="")
    )
    assert env["MODRINTH_MODPACK"] == "cobblemon"
    assert "VERSION" not in env and "MODRINTH_VERSION" not in env and "MODRINTH_LOADER" not in env


def test_curseforge_modpack_env(settings):
    settings.CURSEFORGE_API_KEY = ""
    pack = inst(server_type=ServerType.AUTO_CURSEFORGE, modpack_slug="all-the-mods-10", modpack_version_id="6543210")
    env = build_env(pack)
    assert env["TYPE"] == "AUTO_CURSEFORGE"
    assert env["CF_SLUG"] == "all-the-mods-10"
    assert env["CF_FILE_ID"] == "6543210"
    assert "CF_API_KEY" not in env
    assert "VERSION" not in env
    settings.CURSEFORGE_API_KEY = "env-key"
    assert build_env(pack)["CF_API_KEY"] == "env-key"
    SystemSetting.set_secret("curseforge_api_key", "db-key")
    assert build_env(pack)["CF_API_KEY"] == "db-key"


def test_ftb_modpack_env():
    env = build_env(inst(server_type=ServerType.FTBA, modpack_project_id="119", modpack_version_id="100051"))
    assert env["TYPE"] == "FTBA"
    assert env["FTB_MODPACK_ID"] == "119"
    assert env["FTB_MODPACK_VERSION_ID"] == "100051"
    assert "VERSION" not in env
    assert "FTB_MODPACK_VERSION_ID" not in build_env(inst(server_type=ServerType.FTBA, modpack_project_id="119"))


def test_build_spec_ports_memory_and_secrets():
    instance = inst(
        memory_mb=4096,
        host_ip="192.168.1.10",
        extra_ports=[
            {"container_port": 24454, "host_port": 24454, "protocol": "udp", "host_ip": "", "label": "voice"},
            {"container_port": 8100, "host_port": 8100, "protocol": "tcp", "host_ip": "10.0.0.5"},
        ],
    )
    spec = build_spec(instance)
    assert spec["ports"] == [
        {"container_port": 25565, "host_port": 25600, "protocol": "tcp", "host_ip": "192.168.1.10"},
        {"container_port": 24454, "host_port": 24454, "protocol": "udp", "host_ip": "192.168.1.10"},
        {"container_port": 8100, "host_port": 8100, "protocol": "tcp", "host_ip": "10.0.0.5"},
    ]
    assert spec["memory_limit_mb"] == 4096 + int(4096 * 0.3)  # heap + 30% overhead (non-modded)
    assert spec["rcon_password"] == instance.rcon_password and len(spec["rcon_password"]) >= 16
    assert spec["image"] == "itzg/minecraft-server:stable-java17"
    assert spec["stop_timeout"] == 90
    assert spec["restart_policy"] == "unless-stopped"
    assert "cpu_limit" not in spec and "network" not in spec
    assert "RCON_PASSWORD" not in spec["env"]


def test_container_memory_overhead_rules():
    # non-modded: max(768 MB, 30% of heap)
    assert inst(memory_mb=1024).container_memory_mb == 1024 + 768  # minimum overhead
    assert inst(memory_mb=8192).container_memory_mb == 8192 + int(8192 * 0.3)
    # modded/modpack servers need more off-heap memory: max(1536 MB, 40% of heap)
    assert inst(memory_mb=2048, server_type="FORGE").container_memory_mb == 2048 + 1536
    assert inst(memory_mb=8192, server_type="MODRINTH").container_memory_mb == 8192 + int(8192 * 0.4)
    # explicit overhead wins
    assert inst(memory_mb=2048, memory_overhead_mb=300).container_memory_mb == 2348


def test_build_spec_optional_fields():
    spec = build_spec(inst(cpu_limit=2.5, network="mcpanel_extra"))
    assert spec["cpu_limit"] == 2.5
    assert spec["network"] == "mcpanel_extra"


def test_spec_hash_and_restart_required():
    instance = inst(container_state="running")
    assert restart_required(instance) is False  # never applied
    instance.container_spec_hash = current_spec_hash(instance)
    assert restart_required(instance) is False
    instance.memory_mb = 3072
    assert restart_required(instance) is True
    assert spec_hash({"a": 1, "b": 2}) == spec_hash({"b": 2, "a": 1})


@pytest.mark.parametrize(
    ("container_state", "expected"),
    [("running", True), ("exited", True), ("created", True), ("restarting", True), ("", False)],
)
def test_only_a_server_with_a_container_reads_restart_required(container_state, expected):
    """A stopped server has no container (it is removed once the server is stopped): there is
    nothing to restart, and the next start creates the container from the current settings."""
    instance = inst(container_state=container_state)
    instance.container_spec_hash = current_spec_hash(instance)
    instance.memory_mb = 3072
    assert spec_outdated(instance) is True  # what the jobs compare, whatever the reconciler last saw
    assert restart_required(instance) is expected


def test_container_format_is_hashed_on_the_panel_side_only(monkeypatch):
    from apps.instances import spec as spec_module

    instance = inst()
    spec = build_spec(instance)
    # the agent's spec schema has no such field: the number must not be sent to it
    assert "container_format" not in spec
    assert spec_module.CONTAINER_FORMAT >= 2
    current = spec_hash(spec)
    monkeypatch.setattr(spec_module, "CONTAINER_FORMAT", spec_module.CONTAINER_FORMAT + 1)
    assert spec_hash(spec) != current
    # a container made by the next format reads restart-required under this one, and back
    instance.container_state = "running"
    instance.container_spec_hash = current
    assert restart_required(instance) is True
    instance.container_spec_hash = current_spec_hash(instance)
    assert restart_required(instance) is False


def test_containers_from_before_the_format_number_read_restart_required():
    import hashlib
    import json

    instance = inst()
    # what releases before CONTAINER_FORMAT stored: the hash of the bare spec
    legacy = hashlib.sha256(json.dumps(build_spec(instance), sort_keys=True).encode()).hexdigest()[:16]
    assert legacy != current_spec_hash(instance)
    instance.container_spec_hash = legacy
    instance.container_state = "running"
    assert restart_required(instance) is True
    # a stopped server whose container was removed has nothing to restart ...
    instance.container_state = ""
    assert restart_required(instance) is False and spec_outdated(instance) is True
    # ... and a server that never had a container (or is archived) is not affected either
    instance.container_state = "running"
    instance.container_spec_hash = ""
    assert restart_required(instance) is False


def test_console_pipe_is_panel_owned():
    from apps.instances.spec import env_override_error

    assert build_env(inst())["CREATE_CONSOLE_IN_PIPE"] == "true"
    env = build_env(
        inst(env={"CREATE_CONSOLE_IN_PIPE": "false", "create_console_in_pipe": "0", "CONSOLE_IN_NAMED_PIPE": "/data/x"})
    )
    assert env["CREATE_CONSOLE_IN_PIPE"] == "true"
    assert "create_console_in_pipe" not in env
    # the pipe stays at the image's default path inside the container
    assert "CONSOLE_IN_NAMED_PIPE" not in env
    for key in ("CREATE_CONSOLE_IN_PIPE", "CONSOLE_IN_NAMED_PIPE"):
        assert key in MANAGED_ENV
        assert "managed by MCPanel" in env_override_error(key)


def test_image_for_channels():
    assert image_for(21) == "itzg/minecraft-server:stable-java21"
    SystemSetting.set("runtime_image_channel", "latest")
    assert image_for(8) == "itzg/minecraft-server:java8"
    SystemSetting.set("runtime_image_channel", "pinned:2025.10.1")
    assert image_for(25) == "itzg/minecraft-server:2025.10.1-java25"
    SystemSetting.set("runtime_image_repository", "docker.io/itzg/minecraft-server")
    assert image_for(17) == "docker.io/itzg/minecraft-server:2025.10.1-java17"
