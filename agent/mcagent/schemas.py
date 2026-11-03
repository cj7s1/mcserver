"""Request/response schemas and the validated container spec policy."""

from __future__ import annotations

import ipaddress
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
# Environment keys that the agent owns and refuses to let callers override. The RCON
# password travels in ``rcon_password``; RCON_PASSWORD_FILE would silently replace it
# (and break the agent's own RCON access, which reads RCON_PASSWORD from the container).
RESERVED_ENV = {
    "UID",
    "GID",
    "SERVER_PORT",
    "RCON_PORT",
    "ENABLE_RCON",
    "RCON_PASSWORD",
    "RCON_PASSWORD_FILE",
}
MAX_ENV_VALUE = 32 * 1024


class PortMapping(BaseModel):
    container_port: int = Field(ge=1, le=65535)
    host_port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"] = "tcp"
    host_ip: str = "0.0.0.0"  # noqa: S104 - explicit, validated bind address

    @field_validator("host_ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        ipaddress.ip_address(v)
        return v


class HealthcheckSpec(BaseModel):
    interval_s: int = Field(default=10, ge=2, le=300)
    timeout_s: int = Field(default=5, ge=1, le=120)
    start_period_s: int = Field(default=120, ge=0, le=3600)
    retries: int = Field(default=6, ge=1, le=100)


class InstanceSpec(BaseModel):
    """What the management API may ask the agent to run. Anything not expressible
    here (privileged mode, capabilities, devices, arbitrary mounts) is impossible."""

    name: str = Field(min_length=1, max_length=100)
    image: str = Field(min_length=1, max_length=255)
    env: dict[str, str] = Field(default_factory=dict)
    memory_limit_mb: int = Field(ge=256, le=1024 * 1024)
    cpu_limit: float | None = Field(default=None, gt=0, le=512)
    ports: list[PortMapping] = Field(default_factory=list, max_length=16)
    network: str | None = None
    restart_policy: Literal["no", "unless-stopped", "on-failure"] = "unless-stopped"
    stop_timeout: int = Field(default=60, ge=5, le=900)
    rcon_password: str = Field(min_length=8, max_length=128)
    healthcheck: HealthcheckSpec = Field(default_factory=HealthcheckSpec)

    @field_validator("env")
    @classmethod
    def _env(cls, env: dict[str, str]) -> dict[str, str]:
        if len(env) > 256:
            raise ValueError("Too many environment variables")
        for key, value in env.items():
            if not ENV_KEY_RE.match(key):
                raise ValueError(f"Invalid environment variable name: {key!r}")
            if key.upper() in RESERVED_ENV:
                raise ValueError(f"Environment variable {key} is managed by the agent")
            if "\x00" in value or len(value) > MAX_ENV_VALUE:
                raise ValueError(f"Invalid value for {key}")
        return env

    @model_validator(mode="after")
    def _ports_unique(self) -> InstanceSpec:
        seen: set[tuple[str, int, str]] = set()
        for p in self.ports:
            key = (p.host_ip, p.host_port, p.protocol)
            if key in seen:
                raise ValueError(f"Duplicate host port mapping {p.host_ip}:{p.host_port}/{p.protocol}")
            seen.add(key)
        return self


class ApplySpecRequest(BaseModel):
    spec: InstanceSpec
    start: bool = False
    pull: Literal["missing", "always", "never"] = "missing"


class StopRequest(BaseModel):
    timeout: int | None = Field(default=None, ge=1, le=900)


class CommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=1446)
    timeout: float = Field(default=30.0, gt=0, le=300)


class MkdirRequest(BaseModel):
    path: str
    parents: bool = False


class RenameRequest(BaseModel):
    source: str
    destination: str
    overwrite: bool = False


class DeleteRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)
    recursive: bool = False


class WriteTextRequest(BaseModel):
    path: str
    content: str = Field(max_length=10 * 1024 * 1024)
    create_only: bool = False


class DeleteDataRequest(BaseModel):
    confirm: UUID  # must equal the instance id, a deliberate second key


class BackupRequest(BaseModel):
    backup_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")
    tags: list[str] = Field(default_factory=list, max_length=8)
    consistency: Literal["auto", "require", "none"] = "auto"
    excludes: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("tags", "excludes")
    @classmethod
    def _safe(cls, values: list[str]) -> list[str]:
        for v in values:
            if not re.match(r"^[A-Za-z0-9_.:/*@-]{1,200}$", v) or ".." in v:
                raise ValueError(f"Invalid value {v!r}")
        return values


class RestoreRequest(BaseModel):
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{8,64}$")
    target_instance_id: UUID | None = None  # None => restore in place


class ForgetRequest(BaseModel):
    snapshot_ids: list[str] = Field(default_factory=list, max_length=500)
    prune: bool = True

    @field_validator("snapshot_ids")
    @classmethod
    def _ids(cls, values: list[str]) -> list[str]:
        for v in values:
            if not re.match(r"^[a-f0-9]{8,64}$", v):
                raise ValueError("Invalid snapshot id")
        return values


class CloneRequest(BaseModel):
    target_instance_id: UUID
    exclude_worlds: bool = False


class PrepareInstanceRequest(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)  # relative path -> text content (initial config)


class ExtractRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    destination: str = Field(default="", max_length=4096)
    strip: Literal["auto", "none"] = "auto"
    overwrite: bool = False
    delete_archive: bool = False


class RestorePathRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class SnapshotRequest(BaseModel):
    label: str = Field(default="", max_length=120)
    consistency: Literal["auto", "require", "none"] = "auto"
    kind: Literal["manual", "scheduled", "pre_restore", "pre_update"] = "manual"
