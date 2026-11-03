"""Request schema validation: the agent independently refuses anything outside its policy."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from mcagent.schemas import (
    RESERVED_ENV,
    BackupRequest,
    CommandRequest,
    DeleteDataRequest,
    ForgetRequest,
    InstanceSpec,
    RestoreRequest,
    StopRequest,
)


def spec(**overrides) -> InstanceSpec:
    values = {
        "name": "Survival",
        "image": "itzg/minecraft-server:stable-java21",
        "memory_limit_mb": 2560,
        "rcon_password": "s3cret-rcon-password",
        "ports": [{"container_port": 25565, "host_port": 25565}],
    }
    values.update(overrides)
    return InstanceSpec(**values)


def test_backend_spec_shape_is_accepted():
    """The exact shape apps/instances/spec.py:build_spec produces."""
    s = InstanceSpec(
        **{
            "name": "Survival",
            "image": "itzg/minecraft-server:stable-java17",
            "env": {
                "EULA": "TRUE",
                "TYPE": "VANILLA",
                "VERSION": "1.20.4",
                "MEMORY": "1024M",
                "STOP_DURATION": "70",
                "TZ": "UTC",
            },
            "memory_limit_mb": 1536,
            "ports": [
                {"container_port": 25565, "host_port": 25600, "protocol": "tcp", "host_ip": "0.0.0.0"},
                {"container_port": 24454, "host_port": 24454, "protocol": "udp", "host_ip": "192.168.1.5"},
            ],
            "restart_policy": "unless-stopped",
            "stop_timeout": 90,
            "rcon_password": "x" * 32,
            "cpu_limit": 2.0,
            "network": "mcpanel_instances",
        }
    )
    assert s.healthcheck.retries == 6
    assert s.ports[1].protocol == "udp"


@pytest.mark.parametrize("key", [*sorted(RESERVED_ENV), "uid", "Server_Port", "rcon_password_file"])
def test_reserved_env_keys_rejected(key):
    with pytest.raises(ValidationError, match="managed by the agent"):
        spec(env={key: "0"})


def test_rcon_password_overrides_are_reserved():
    assert {"RCON_PASSWORD", "RCON_PASSWORD_FILE", "UID", "GID"} <= RESERVED_ENV


@pytest.mark.parametrize("key", ["1ABC", "A-B", "A B", "", "A=B", "Ä", "x" * 129])
def test_invalid_env_names(key):
    with pytest.raises(ValidationError, match="Invalid environment variable name"):
        spec(env={key: "v"})


def test_env_values_and_count():
    with pytest.raises(ValidationError, match="Invalid value"):
        spec(env={"MOTD": "a\x00b"})
    with pytest.raises(ValidationError, match="Invalid value"):
        spec(env={"MOTD": "x" * (32 * 1024 + 1)})
    with pytest.raises(ValidationError, match="Too many"):
        spec(env={f"VAR_{i}": "1" for i in range(257)})
    assert spec(env={"MOTD": "multi\nline ok"}).env["MOTD"] == "multi\nline ok"


@pytest.mark.parametrize(
    "port",
    [
        {"container_port": 25565, "host_port": 0},
        {"container_port": 25565, "host_port": 65536},
        {"container_port": 0, "host_port": 25565},
        {"container_port": 25565, "host_port": 25565, "protocol": "sctp"},
        {"container_port": 25565, "host_port": 25565, "host_ip": "999.1.1.1"},
        {"container_port": 25565, "host_port": 25565, "host_ip": "localhost"},
        {"container_port": "abc", "host_port": 25565},
    ],
)
def test_bad_ports(port):
    with pytest.raises(ValidationError):
        spec(ports=[port])


def test_duplicate_host_ports_rejected():
    with pytest.raises(ValidationError, match="Duplicate host port"):
        spec(
            ports=[
                {"container_port": 25565, "host_port": 25565},
                {"container_port": 25566, "host_port": 25565},
            ]
        )
    # same port on another protocol or bind address is a different socket
    ok = spec(
        ports=[
            {"container_port": 25565, "host_port": 25565},
            {"container_port": 25565, "host_port": 25565, "protocol": "udp"},
            {"container_port": 25565, "host_port": 25565, "host_ip": "127.0.0.1"},
        ]
    )
    assert len(ok.ports) == 3


def test_port_count_limit():
    with pytest.raises(ValidationError):
        spec(ports=[{"container_port": 25565, "host_port": 30000 + i} for i in range(17)])


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_limit_mb": 255},
        {"memory_limit_mb": 1024 * 1024 + 1},
        {"cpu_limit": 0},
        {"cpu_limit": 513},
        {"restart_policy": "always"},
        {"stop_timeout": 4},
        {"stop_timeout": 901},
        {"rcon_password": "short"},
        {"name": ""},
        {"image": ""},
        {"healthcheck": {"interval_s": 1}},
    ],
)
def test_resource_and_policy_bounds(overrides):
    with pytest.raises(ValidationError):
        spec(**overrides)


def test_dangerous_options_cannot_be_expressed():
    s = spec(privileged=True, cap_add=["SYS_ADMIN"], devices=["/dev/sda"], volumes={"/": {"bind": "/host"}})
    dumped = s.model_dump()
    for key in ("privileged", "cap_add", "devices", "volumes"):
        assert key not in dumped


def test_backup_request_validation():
    BackupRequest(
        backup_id=str(uuid.uuid4()), tags=["kind:manual"], excludes=["logs/*.log.gz", "modernfix/classCache*"]
    )
    for bad in (
        {"backup_id": "has space"},
        {"backup_id": "x" * 65},
        {"backup_id": "ok", "tags": ["a b"]},
        {"backup_id": "ok", "tags": ["--password-file=/etc/shadow"]},
        {"backup_id": "ok", "excludes": ["../../etc"]},
        {"backup_id": "ok", "excludes": ["$(rm -rf /)"]},
        {"backup_id": "ok", "consistency": "maybe"},
    ):
        with pytest.raises(ValidationError):
            BackupRequest(**bad)


def test_snapshot_id_validation():
    RestoreRequest(snapshot_id="a1b2c3d4")
    ForgetRequest(snapshot_ids=["a1b2c3d4" * 8])
    for bad in ("A1B2C3D4", "abc", "a1b2c3d4;rm", "g" * 8, "--json"):
        with pytest.raises(ValidationError):
            RestoreRequest(snapshot_id=bad)
        with pytest.raises(ValidationError):
            ForgetRequest(snapshot_ids=[bad])


def test_misc_requests():
    with pytest.raises(ValidationError):
        DeleteDataRequest(confirm="not-a-uuid")
    with pytest.raises(ValidationError):
        CommandRequest(command="")
    with pytest.raises(ValidationError):
        CommandRequest(command="x" * 1447)
    with pytest.raises(ValidationError):
        CommandRequest(command="list", timeout=0)
    with pytest.raises(ValidationError):
        StopRequest(timeout=901)
    assert StopRequest().timeout is None
