"""Minecraft (Java edition on itzg/minecraft-server): the first game definition.

A wrapper around today's code, not a copy of it: the container spec is ``spec.build_minecraft_spec``
(the exact dict and hash of the releases before game definitions), the config file is
server.properties (``instances/properties.py``), the world model is ``instances/world.py``, the
players are ``players/service.py``, the log analysis is ``instances/diagnostics.py``. The
capabilities reproduce the gating that existed before: everything, except ``content`` for server
types without mods or plugins (``Instance.content_kind`` "none") and ``modpacks`` for servers
that are not modpack servers (``Instance.is_modpack``).
"""

from __future__ import annotations

import re
from types import ModuleType
from typing import TYPE_CHECKING, Any

from .base import CAPABILITIES, ConfigFile, GameDefinition, Variant

MASK = "********"
_RCON_LINE = re.compile(r"(?m)^(rcon\.password\s*[=:]\s*)(.*?)\r?$")

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient

# The server types (the itzg TYPE values) are the variants; same labels as ServerType.
VARIANTS = (
    Variant("VANILLA", "Vanilla"),
    Variant("PAPER", "Paper"),
    Variant("PURPUR", "Purpur"),
    Variant("FOLIA", "Folia"),
    Variant("FABRIC", "Fabric"),
    Variant("QUILT", "Quilt"),
    Variant("FORGE", "Forge"),
    Variant("NEOFORGE", "NeoForge"),
    Variant("MODRINTH", "Modrinth modpack"),
    Variant("AUTO_CURSEFORGE", "CurseForge modpack"),
    Variant("FTBA", "Feed The Beast modpack"),
    Variant("CUSTOM", "Custom server jar"),
)

# Commands that run another command, given as the rest of the line: ``execute … run <command>`` and
# (since 1.20.2) ``return run <command>``, also under the ``minecraft:`` namespace that Paper/Spigot
# register; and the stop command with that namespace (vanilla answers "Unknown or incomplete command"
# for ``minecraft:stop``, Paper/Spigot run it).
RUN_COMMANDS = frozenset({"execute", "minecraft:execute", "return", "minecraft:return"})
STOP_TARGETS = frozenset({"stop", "minecraft:stop"})


class MinecraftDefinition(GameDefinition):
    id = "minecraft"
    label = "Minecraft"
    variants = VARIANTS
    profile = "minecraft"
    control = "minecraft-rcon"
    memory_model = "jvm"
    capability_set = frozenset(CAPABILITIES)
    stop_words = frozenset({"stop"})
    # "stop" over RCON exits the server and Docker's restart policy starts it again at once: the REST
    # command endpoint and schedules refuse it (400 stop_via_power), the console already turns it
    # into the panel's Stop (real-stack run of 2026-11-11; a deliberate Minecraft change)
    refuse_stop_commands = True
    flush_command = "save-all flush"
    notes = (
        "Java edition, run on the itzg/minecraft-server image.",
        "Memory is the Java heap; the container gets extra room for off-heap memory.",
    )

    def default_memory_mb(self) -> int:
        from apps.core.models import SystemSetting

        return int(SystemSetting.get("default_memory_mb") or 4096)

    def capabilities(self, instance: Instance | None = None) -> frozenset[str]:
        if instance is None:
            return self.capability_set
        caps = set(self.capability_set)
        if instance.content_kind == "none":
            caps.discard("content")
        if not instance.is_modpack:
            caps.discard("modpacks")
        return frozenset(caps)

    def config_files(self) -> tuple[ConfigFile, ...]:
        from apps.instances import properties

        return (
            ConfigFile(
                path="server.properties",
                codec="java-properties",
                label="Server properties",
                managed_keys=frozenset(properties.MANAGED_KEYS),
                secret_keys=("rcon.password", "management-server-secret"),
            ),
        )

    # -- container ---------------------------------------------------------------------------
    def build_spec(self, instance: Instance) -> dict[str, Any]:
        from apps.instances.spec import build_minecraft_spec

        return build_minecraft_spec(instance)

    def container_memory_mb(self, instance: Instance) -> int:
        return instance.jvm_container_memory_mb

    def initial_files(self, instance: Instance, params: dict[str, Any]) -> dict[str, str]:
        from apps.instances import properties

        initial = params.get("server_properties") or {}
        return {"server.properties": properties.render(initial)} if initial else {}

    # -- console -------------------------------------------------------------------------------
    def normalise_command(self, text: str) -> str:
        # RCON takes the command without the chat prefix the players type
        return text.lstrip("/")

    def is_stop_command(self, text: str) -> bool:
        """``stop`` (any case, leading slashes, blanks), and a command whose final ``run`` target is
        stop: ``execute … run stop``, nested (``execute as @a run execute run /stop``),
        ``return run stop``, ``minecraft:stop``. The stop command takes no arguments, so a line that
        makes the server exit through ``run`` ends with ``run stop``; checking the end of the line
        (and not the first ``run`` word) cannot be fooled by quoted or bracketed arguments that hold
        the word ``run``. A line that only mentions the words (``say stop``, ``execute run say stop``,
        ``tellraw @a "run stop"``) is no stop command."""
        words = text.strip().lstrip("/").strip().lower().split()
        if len(words) == 1:
            return words[0] in STOP_TARGETS
        return len(words) >= 3 and words[0] in RUN_COMMANDS and words[-2] == "run" and words[-1].lstrip("/") in STOP_TARGETS

    def broadcast(self, client: AgentClient, instance: Instance, text: str, *, message: str = "", color: str = "yellow") -> None:
        from apps.instances.power_warnings import UNKNOWN_COMMAND, tellraw_command

        out = client.command(instance.id, tellraw_command(text, message, color=color))
        if out and UNKNOWN_COMMAND.search(out):
            # very old or unusual servers without tellraw: fixed text only (no operator message,
            # because `say` expands selectors such as @a)
            client.command(instance.id, f"say {text}")

    def online_players(self, instance: Instance) -> dict[str, Any]:
        from apps.players.service import online_players

        return online_players(instance)

    def analyze_logs(self, instance: Instance, lines: list[str]) -> list[Any]:
        from apps.instances.diagnostics import analyze_logs

        return analyze_logs(instance, lines)

    # -- files that hold the RCON password (C-2) ------------------------------------------------------------------
    # itzg writes the RCON password (the control secret) into server.properties and the rcon-cli config: the
    # Properties tab masked it, the file manager showed it to anyone with instance.files.read
    masks_by_line = True

    def secret_bearing_paths(self, instance: Instance) -> tuple[str, ...]:
        return ("server.properties", ".rcon-cli.env", ".rcon-cli.yaml")

    def masked_text(self, instance: Instance, path: str, content: str) -> str:
        from apps.instances.spec import rcon_password

        secret = rcon_password(instance)
        return content.replace(secret, MASK) if secret and len(secret) >= 8 else content

    def unmask_text(self, instance: Instance, path: str, content: str, current: str) -> str:
        """server.properties saved from the editor keeps its RCON password where the editor showed the mask."""
        if path.strip("/") != "server.properties" or MASK not in content:
            return content
        values = dict(_RCON_LINE.findall(current or ""))

        def restore(m: re.Match[str]) -> str:
            return f"{m.group(1)}{values[m.group(1)]}" if m.group(2) == MASK and m.group(1) in values else m.group(0)

        return _RCON_LINE.sub(restore, content)

    # -- worlds and players ----------------------------------------------------------------------
    @property
    def world_model(self) -> ModuleType:
        from apps.instances import world

        return world

    @property
    def players_service(self) -> ModuleType:
        from apps.players import service

        return service

    # -- software record of backups ------------------------------------------------------------
    @property
    def software_keys(self) -> tuple[str, ...]:  # type: ignore[override]
        from apps.instances.handlers import SOFTWARE_FIELDS

        return ("game", *SOFTWARE_FIELDS)

    def software_state(self, instance: Instance) -> dict[str, Any]:
        from apps.instances.handlers import SOFTWARE_FIELDS

        return {"game": self.id, **{f: getattr(instance, f) for f in SOFTWARE_FIELDS}}

    def describe_software(self, state: dict[str, Any]) -> str:
        from apps.instances.handlers import describe_minecraft_software

        return describe_minecraft_software(state)
