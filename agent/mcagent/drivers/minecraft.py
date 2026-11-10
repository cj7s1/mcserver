"""``minecraft-rcon``: Minecraft Java Edition in itzg/minecraft-server, as the agent has always
controlled it.

RCON on container port 25575 with the password read back from the container's ``RCON_PASSWORD``;
``save-all flush`` before a stop; ``stop`` delivered by the agent when RCON was not open yet at
the stop; ``save-off`` + ``save-all flush`` / ``save-on`` around backups and snapshots (the marker
files are ``main._world_saving_paused``'s); the server list ping on the game port 25565/tcp.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from .. import slp
from ..errors import AgentError
from .rcon import RconCommands, RconDriver, RconStep, _ops

if TYPE_CHECKING:
    from docker.models.containers import Container

    from ..config import Settings

GAME_PORT = 25565
RCON_PORT = 25575
SECRET_ENV = "RCON_PASSWORD"  # noqa: S105 - the name of the variable, not a password
SAVE_BEFORE_STOP = "save-all flush"
STOP_COMMAND = "stop"

COMMANDS = RconCommands(
    save_before_stop=SAVE_BEFORE_STOP,
    stop=STOP_COMMAND,
    save=(RconStep("save-all flush", 300, "Flushing world to disk (save-all flush)"),),
    pause_saving=(
        RconStep("save-off", 30, "Pausing world saving (save-off)"),
        RconStep("save-all flush", 300, "Flushing world to disk (save-all flush)"),
    ),
    resume_saving=(RconStep("save-on", 30, "save-on"),),
)


class MinecraftDriver(RconDriver):
    id: ClassVar[str] = "minecraft-rcon"

    def __init__(self) -> None:
        super().__init__(port=RCON_PORT, secret_env=SECRET_ENV, commands=COMMANDS)

    def query_endpoint(self, settings: Settings, c: Container) -> tuple[str, int]:
        """(host, port) of the running server's game port as the agent reaches it (server list ping)."""
        info = _ops().describe(c)
        if settings.rcon_mode == "published":
            for p in info["ports"]:
                if p["container_port"] == GAME_PORT and p["protocol"] == "tcp":
                    return "127.0.0.1", p["host_port"]
            raise AgentError("The game port is not published", "ping_unavailable", 409)
        if c.attrs.get("HostConfig", {}).get("NetworkMode") == "host":
            return "127.0.0.1", GAME_PORT
        if not info["ip_address"]:
            raise AgentError("Container has no IP address", "ping_unavailable", 409)
        return info["ip_address"], GAME_PORT

    async def query(self, host: str, port: int, timeout: float) -> dict[str, Any]:
        """Server list ping: players online, max, sample, version, motd, latency."""
        return await slp.ping(host, port, timeout=timeout)
