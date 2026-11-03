"""Agent configuration (environment variables prefixed with ``AGENT_``)."""

from __future__ import annotations

import json
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    # Shared secret the management API must present as a Bearer token.
    token: str = ""
    token_file: Path | None = None

    # Root of all agent-managed persistent data *as seen by the agent process*.
    data_root: Path = Path("/srv/mcpanel")
    # The same directory as seen by the Docker daemon (bind-mount source). When the
    # agent runs inside a container this must be the host path; mount the host path
    # at the same location inside the agent to keep them identical.
    host_data_root: Path | None = None
    # Optional separate root for backup repositories (e.g. array vs cache on Unraid).
    backup_root: Path | None = None

    # Container runtime policy ---------------------------------------------------
    allowed_images: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["itzg/minecraft-server", "docker.io/itzg/minecraft-server"]
    )
    instance_network: str = "mcpanel_instances"
    allowed_networks: Annotated[list[str], NoDecode] = Field(default_factory=list)
    allow_host_network: bool = False
    port_min: int = 1024
    port_max: int = 65535
    max_memory_mb: int = 65536
    pids_limit: int = 8192
    log_max_size: str = "20m"
    log_max_files: int = 5
    runtime_uid: int = 1000
    runtime_gid: int = 1000
    # Extra environment injected into every Minecraft container (e.g. HTTPS_PROXY on
    # networks that require an egress proxy). JSON object string.
    runtime_extra_env: dict[str, str] = Field(default_factory=dict)
    # Optional CA bundle / Java truststore files mounted read-only into containers
    # (for TLS-intercepting egress proxies). Paths are on the *host*.
    runtime_ca_bundle: Path | None = None
    runtime_java_truststore: Path | None = None
    # How the agent reaches RCON: "container_ip" (agent shares the instance network or
    # runs on the host) or "published" (connect to 127.0.0.1:<published rcon port>).
    rcon_mode: str = "container_ip"

    # API listener (used by ``python -m mcagent``) --------------------------------
    listen_host: str = "0.0.0.0"  # noqa: S104 - inside the agent container; see docs
    listen_port: int = 8700
    # TLS for the agent API: "off", "auto" (self-signed, pinned by the panel) or "files".
    tls: str = "off"
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    # Extra DNS names / IPs for the self-signed certificate's SAN (informational: the
    # panel pins the certificate itself, so names do not have to match).
    tls_hostnames: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Backups ---------------------------------------------------------------------
    restic_binary: str = "restic"
    restic_password: str = ""
    restic_password_file: Path | None = None

    @field_validator("allowed_images", "allowed_networks", "tls_hostnames", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("["):
                return json.loads(value)
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @field_validator("tls")
    @classmethod
    def _tls_mode(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ("off", "auto", "files"):
            raise ValueError("AGENT_TLS must be off, auto or files")
        return value

    @field_validator("runtime_extra_env", mode="before")
    @classmethod
    def _parse_env(cls, value: object) -> object:
        if isinstance(value, str):
            return json.loads(value) if value.strip() else {}
        return value

    # Derived paths ---------------------------------------------------------------
    @property
    def instances_dir(self) -> Path:
        return self.data_root / "instances"

    @property
    def host_instances_dir(self) -> Path:
        return (self.host_data_root or self.data_root) / "instances"

    @property
    def backups_dir(self) -> Path:
        return self.backup_root or (self.data_root / "backups")

    @property
    def staging_dir(self) -> Path:
        return self.data_root / "staging"

    @property
    def trash_dir(self) -> Path:
        return self.data_root / "trash"

    @property
    def state_dir(self) -> Path:
        return self.data_root / "agent"

    def effective_networks(self) -> set[str]:
        nets = set(self.allowed_networks) | {self.instance_network}
        if self.allow_host_network:
            nets.add("host")
        return nets

    def resolve_token(self) -> str:
        if self.token_file:
            return self.token_file.read_text().strip()
        return self.token

    def resolve_restic_password(self) -> str:
        if self.restic_password_file and self.restic_password_file.exists():
            return self.restic_password_file.read_text().strip()
        if self.restic_password:
            return self.restic_password
        # Generate and persist one on first use so backups are always encrypted.
        path = self.state_dir / "restic-password"
        if path.exists():
            return path.read_text().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        pw = secrets.token_urlsafe(32)
        path.write_text(pw)
        path.chmod(0o600)
        return pw


@lru_cache
def get_settings() -> Settings:
    return Settings()
