"""Agent configuration (environment variables prefixed with ``AGENT_``)."""

from __future__ import annotations

import contextlib
import json
import os
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
    # Optional location of rebuildable caches (file digests). Defaults to <state dir>/cache.
    cache_root: Path | None = None

    # Container runtime policy ---------------------------------------------------
    allowed_images: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["itzg/minecraft-server", "docker.io/itzg/minecraft-server"]
    )
    instance_network: str = "mcpanel_instances"
    # One bridge network per server (A-1, pernet.py): a spec without a network gets its own bridge
    # zz_mcpanel_i_<id12> with a /29 of this pool (AGENT_PER_SERVER_NETWORKS, AGENT_INSTANCE_POOL). Off,
    # or when the agent cannot use it, every server is on the shared instance network as before.
    per_server_networks: bool = True
    instance_pool: str = "10.213.0.0/16"
    # Repair the network of the platform's own containers (the agent's compose project) when Docker dropped
    # it after a start that failed on a published port (AGENT_REPAIR_PLATFORM_NETWORKS; platformnet.py).
    repair_platform_networks: bool = True
    allowed_networks: Annotated[list[str], NoDecode] = Field(default_factory=list)
    allow_host_network: bool = False
    # Refuse API requests from the gateway address of every bridge network the agent is attached to
    # (AGENT_REFUSE_BRIDGE_GATEWAYS). A game container that calls a published agent port through the
    # host is NATed to such a gateway (A-4). Switch it off only when the agent is called from the
    # host itself through a bridge address, or when Docker's userland proxy (rootless Docker,
    # iptables off) hands remote clients to the agent from the gateway.
    refuse_bridge_gateways: bool = True
    # Check the digest of a version tag a runtime profile pins (``tag_digests``, A-8) before a container
    # is created from it (AGENT_IMAGE_DIGEST_CHECK). Switch it off only to run a tag the maintainer
    # re-pushed, after checking the new image yourself.
    image_digest_check: bool = True
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
    # Upper limit, in seconds, for the stop timeout Docker itself gets for a Minecraft
    # container (Config.StopTimeout = min(the server's stop timeout, this value)). Docker
    # uses it whenever it stops a server on its own: host shutdown, daemon restart,
    # `docker stop` / `docker restart` without a time. Stops from the panel are not affected
    # and keep the server's full stop timeout.
    # Why it is capped: at array stop or shutdown Unraid asks the Docker daemon to stop and
    # kills it 30 s later, and the daemon waits for max(StopTimeout of its containers) + 5 s
    # for them. 20 + 5 s fits inside those 30 s; with a larger value a slow server would
    # still be stopping when the daemon is killed. Raise it only on hosts that give the
    # daemon more time.
    shutdown_stop_timeout: int = Field(default=20, ge=5, le=600)

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
    def host_state_dir(self) -> Path:
        """:attr:`state_dir` as the Docker daemon sees it (bind sources of agent-written files)."""
        return (self.host_data_root or self.data_root) / "agent"

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

    @property
    def cache_dir(self) -> Path:
        return self.cache_root or (self.state_dir / "cache")

    @property
    def hash_cache_dir(self) -> Path:
        return self.cache_dir / "file-hashes"

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
        write_private(path, pw)
        return pw


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with mode 0600 from the start (never readable by others in between):
    a temporary file created 0600 without following links, then renamed over ``path`` (B-13)."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")  # one per writer: callers may race
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@lru_cache
def get_settings() -> Settings:
    return Settings()
