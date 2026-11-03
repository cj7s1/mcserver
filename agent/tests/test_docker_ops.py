"""Container policy: image allow-list, network/port/memory policy and the security properties
of the Docker create() arguments. No Docker daemon is needed."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from mcagent import docker_ops
from mcagent.config import Settings
from mcagent.docker_ops import AgentError, build_create_kwargs, split_image, validate_image
from mcagent.schemas import InstanceSpec

IID = uuid.UUID("0123abcd-0000-4000-8000-00000000beef")

# Every key build_create_kwargs may produce. Anything new must be reviewed for safety.
ALLOWED_KWARGS = {
    "image",
    "name",
    "hostname",
    "environment",
    "labels",
    "volumes",
    "ports",
    "network",
    "mem_limit",
    "memswap_limit",
    "pids_limit",
    "restart_policy",
    "stop_signal",
    "stdin_open",
    "tty",
    "detach",
    "security_opt",
    "cap_drop",
    "cap_add",
    "log_config",
    "healthcheck",
    "nano_cpus",
}
MINIMAL_CAPS = {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL"}


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    # isolate from a shell that has an agent env file sourced (CA bundle, extra env, ...)
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    return Settings(data_root=tmp_path / "data", host_data_root=Path("/mnt/cache/appdata/mcpanel"), token="t")


def spec(**overrides) -> InstanceSpec:
    values = {
        "name": "Survival",
        "image": "itzg/minecraft-server:stable-java21",
        "memory_limit_mb": 2560,
        "rcon_password": "s3cret-rcon-password",
        "env": {"TYPE": "PAPER", "VERSION": "1.21.1", "EULA": "TRUE"},
        "ports": [
            {"container_port": 25565, "host_port": 25570},
            {"container_port": 24454, "host_port": 24454, "protocol": "udp", "host_ip": "192.168.1.5"},
        ],
    }
    values.update(overrides)
    return InstanceSpec(**values)


# --- images ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("itzg/minecraft-server:java21", ("itzg/minecraft-server", "java21")),
        ("itzg/minecraft-server", ("itzg/minecraft-server", "latest")),
        (
            "docker.io/itzg/minecraft-server:2025.10.1-java25",
            ("docker.io/itzg/minecraft-server", "2025.10.1-java25"),
        ),
        ("registry:5000/itzg/minecraft-server", ("registry:5000/itzg/minecraft-server", "latest")),
        ("registry:5000/itzg/minecraft-server:java17", ("registry:5000/itzg/minecraft-server", "java17")),
    ],
)
def test_split_image(image, expected):
    assert split_image(image) == expected


def test_validate_image_allow_list(settings):
    assert (
        validate_image(settings, "itzg/minecraft-server:stable-java17")
        == "itzg/minecraft-server:stable-java17"
    )
    assert (
        validate_image(settings, "docker.io/itzg/minecraft-server:java8")
        == "docker.io/itzg/minecraft-server:java8"
    )
    assert validate_image(settings, "itzg/minecraft-server") == "itzg/minecraft-server:latest"


@pytest.mark.parametrize(
    "image",
    [
        "evil/minecraft-server:java21",
        "itzg/minecraft-server-fork:java21",
        "ghcr.io/itzg/minecraft-server:java21",
        "registry:5000/itzg/minecraft-server:java21",
        "itzg/minecraft-server:java21:extra",
        "alpine",
    ],
)
def test_images_outside_allow_list_rejected(settings, image):
    with pytest.raises(AgentError) as exc:
        validate_image(settings, image)
    assert exc.value.code == "image_not_allowed"
    assert exc.value.status == 403


@pytest.mark.parametrize(
    "image",
    ["itzg/minecraft-server@sha256:" + "a" * 64, "itzg/minecraft-server:-bad", "itzg/minecraft-server:a b"],
)
def test_bad_image_references(settings, image):
    with pytest.raises(AgentError) as exc:
        validate_image(settings, image)
    assert exc.value.code in ("invalid_image", "image_not_allowed")


def test_allow_list_is_configurable(tmp_path):
    custom = Settings(data_root=tmp_path, allowed_images="ghcr.io/me/mc, itzg/minecraft-server")
    assert custom.allowed_images == ["ghcr.io/me/mc", "itzg/minecraft-server"]
    assert validate_image(custom, "ghcr.io/me/mc:1") == "ghcr.io/me/mc:1"


# --- create kwargs: security properties --------------------------------------------------------------
def test_create_kwargs_security_properties(settings):
    kw = build_create_kwargs(settings, IID, spec(), "hash123")
    assert set(kw) <= ALLOWED_KWARGS
    assert kw["cap_drop"] == ["ALL"]
    assert set(kw["cap_add"]) == MINIMAL_CAPS
    assert "privileged" not in kw and "devices" not in kw and "pid_mode" not in kw and "ipc_mode" not in kw
    assert "no-new-privileges:true" in kw["security_opt"]
    assert kw["network"] == "mcpanel_instances"
    # exactly one bind mount: the instance directory (host path) at /data
    assert kw["volumes"] == {
        "/mnt/cache/appdata/mcpanel/instances/0123abcd-0000-4000-8000-00000000beef": {
            "bind": "/data",
            "mode": "rw",
        }
    }
    # memory: hard limit, no swap; bounded pids; rotated logs
    assert kw["mem_limit"] == "2560m" and kw["memswap_limit"] == "2560m"
    assert kw["pids_limit"] == settings.pids_limit
    assert kw["log_config"] == {"Type": "json-file", "Config": {"max-size": "20m", "max-file": "5"}}
    assert kw["detach"] is True and kw["tty"] is False


def test_create_kwargs_healthcheck_labels_and_identity(settings):
    kw = build_create_kwargs(settings, IID, spec(stop_timeout=120), "hash123")
    assert kw["healthcheck"] == {
        "test": ["CMD", "mc-health"],
        "interval": 10_000_000_000,
        "timeout": 5_000_000_000,
        "start_period": 120_000_000_000,
        "retries": 6,
    }
    assert kw["labels"] == {
        "io.mcpanel.managed": "true",
        "io.mcpanel.instance": str(IID),
        "io.mcpanel.name": "Survival",
        "io.mcpanel.spec-hash": "hash123",
        "io.mcpanel.stop-timeout": "120",
    }
    assert kw["name"] == "mcpanel-0123abcd0000"
    assert kw["hostname"] == "mc-0123abcd"
    assert kw["restart_policy"] == {"Name": "unless-stopped"}
    assert build_create_kwargs(settings, IID, spec(restart_policy="on-failure"), "h")["restart_policy"] == {
        "Name": "on-failure",
        "MaximumRetryCount": 3,
    }


def test_create_kwargs_ports(settings):
    kw = build_create_kwargs(settings, IID, spec(), "h")
    assert kw["ports"] == {"25565/tcp": [("0.0.0.0", 25570)], "24454/udp": [("192.168.1.5", 24454)]}
    assert "25575/tcp" not in kw["ports"]  # RCON is never published


def test_agent_owned_env_always_wins(settings):
    settings.runtime_extra_env = {"HTTPS_PROXY": "http://proxy:3128", "UID": "0"}
    # model_copy(update=...) bypasses validation: the agent's own values must still win
    unchecked = spec().model_copy(
        update={"env": {"UID": "0", "RCON_PASSWORD": "x", "SERVER_PORT": "1", "MOTD": "hi"}}
    )
    env = build_create_kwargs(settings, IID, unchecked, "h")["environment"]
    assert env["UID"] == str(settings.runtime_uid) and env["GID"] == str(settings.runtime_gid)
    assert env["RCON_PASSWORD"] == "s3cret-rcon-password"
    assert env["RCON_PORT"] == "25575" and env["SERVER_PORT"] == "25565" and env["ENABLE_RCON"] == "true"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["MOTD"] == "hi"


def test_eula_comes_from_spec(settings):
    env = build_create_kwargs(settings, IID, spec(env={"EULA": "FALSE"}), "h")["environment"]
    assert env["EULA"] == "FALSE"


def test_optional_certificate_mounts_are_read_only(settings, tmp_path):
    settings.runtime_ca_bundle = Path("/etc/ssl/certs/ca-certificates.crt")
    settings.runtime_java_truststore = Path("/etc/ssl/certs/java/cacerts")
    vols = build_create_kwargs(settings, IID, spec(), "h")["volumes"]
    assert vols["/etc/ssl/certs/ca-certificates.crt"] == {
        "bind": "/mcpanel/certs/ca-bundle.crt",
        "mode": "ro",
    }
    assert vols["/etc/ssl/certs/java/cacerts"] == {"bind": "/mcpanel/certs/cacerts", "mode": "ro"}
    assert len(vols) == 3
    assert [v for v in vols.values() if v["mode"] == "rw"] == [{"bind": "/data", "mode": "rw"}]


def test_cpu_limit(settings):
    assert build_create_kwargs(settings, IID, spec(cpu_limit=1.5), "h")["nano_cpus"] == 1_500_000_000
    assert "nano_cpus" not in build_create_kwargs(settings, IID, spec(), "h")


# --- policy ----------------------------------------------------------------------------------------
def test_network_policy(settings):
    for net in ("host", "bridge", "some_other_project"):
        with pytest.raises(AgentError) as exc:
            build_create_kwargs(settings, IID, spec(network=net), "h")
        assert exc.value.code == "network_not_allowed" and exc.value.status == 403
    settings.allowed_networks = ["proxy_net"]
    assert build_create_kwargs(settings, IID, spec(network="proxy_net"), "h")["network"] == "proxy_net"


def test_host_network_only_when_enabled_and_never_publishes_ports(settings):
    settings.allow_host_network = True
    kw = build_create_kwargs(settings, IID, spec(network="host"), "h")
    assert kw["network"] == "host"
    assert kw["ports"] == {}


def test_port_range_policy(settings):
    settings.port_min, settings.port_max = 25500, 25700
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, spec(), "h")  # 24454 is below the range
    assert exc.value.code == "port_not_allowed"
    build_create_kwargs(settings, IID, spec(ports=[{"container_port": 25565, "host_port": 25600}]), "h")


def test_memory_policy(settings):
    settings.max_memory_mb = 2048
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, spec(memory_limit_mb=4096), "h")
    assert exc.value.code == "memory_limit"


def test_image_policy_applies_to_create(settings):
    with pytest.raises(AgentError) as exc:
        build_create_kwargs(settings, IID, spec(image="ubuntu:24.04"), "h")
    assert exc.value.code == "image_not_allowed"


# --- describe() -----------------------------------------------------------------------------------
class FakeContainer:
    def __init__(self, attrs, labels):
        self.attrs = attrs
        self.labels = labels
        self.id = "c0ffee"
        self.name = "mcpanel-0123abcd0000"


def test_describe_parses_docker_state():
    attrs = {
        "Config": {"Image": "itzg/minecraft-server:java21"},
        "State": {
            "Status": "running",
            "Running": True,
            "Restarting": False,
            "ExitCode": 0,
            "OOMKilled": False,
            "Error": "",
            "StartedAt": "2026-11-03T17:00:00.123456789Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
            "Health": {"Status": "healthy", "Log": [{"Output": "motd=Hello online=0 max=20"}]},
        },
        "RestartCount": 2,
        "NetworkSettings": {
            "Networks": {"mcpanel_instances": {"IPAddress": "172.30.0.5"}},
            "Ports": {"25565/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25570"}], "25575/tcp": None},
        },
        "Created": "2026-11-03T16:59:00Z",
    }
    d = docker_ops.describe(FakeContainer(attrs, {"io.mcpanel.spec-hash": "abc"}))
    assert d["status"] == "running" and d["running"] is True and d["health"] == "healthy"
    assert d["started_at"] == "2026-11-03T17:00:00.123456789Z"
    assert d["finished_at"] is None
    assert d["ip_address"] == "172.30.0.5"
    assert d["ports"] == [
        {"container_port": 25565, "protocol": "tcp", "host_ip": "0.0.0.0", "host_port": 25570}
    ]
    assert d["spec_hash"] == "abc" and d["restart_count"] == 2
    assert d["health_output"].startswith("motd=Hello")
    assert d["error"] is None


def test_foreign_container_with_our_name_is_refused(monkeypatch):
    class Client:
        class containers:
            @staticmethod
            def get(name):
                return FakeContainer(
                    {}, {"io.mcpanel.managed": "true", "io.mcpanel.instance": "someone-else"}
                )

    monkeypatch.setattr(docker_ops, "client", lambda: Client())
    with pytest.raises(AgentError) as exc:
        docker_ops.get_container(IID)
    assert exc.value.code == "foreign_container"
