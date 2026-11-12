"""How a friend joins a server, per game (B1 of the feature plan of 2026-11-12: the Share card).

``guide(instance, host, port)`` gives the plain-language steps in the game's client, what the friend needs
(version, mods), how the server keeps strangers out (``protection``: a join password, a whitelist, accounts or
nothing) and the warning when it is open by default (docs/GAMES.md, "Who can join by default").

``join_password(instance, client)`` reads the server's join password where the panel keeps it (a secret, or a
setting in the game's own file); None when it has none or it cannot be read now. Only callers with
``instance.configure`` ever see it (apps.instances.share).

Steps marked ``# general knowledge`` in the code are not covered by the repository's own docs; the notes of the
feature list them for the owner to check against the game.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient


@dataclass
class Guide:
    steps: list[str]
    requirements: list[str] = field(default_factory=list)
    # how strangers are kept out: "password", "whitelist", "accounts" (Project Zomboid) or "none"
    protection: str = "password"
    # who can join while that protection is off (docs/GAMES.md)
    open_warning: str = ""


def _hostport(host: str, port: int, default: int | None = None) -> str:
    return host if port == default else f"{host}:{port}"


def _minecraft(instance: Instance, host: str, port: int) -> Guide:
    from apps.instances.models import ServerType

    address = _hostport(host, port, 25565)
    version = str(instance.minecraft_version or "")
    requirements = [
        "Minecraft: Java Edition, the newest release"
        if version.upper() in ("", "LATEST")
        else f"Minecraft: Java Edition {version}"
    ]
    if instance.modpack_name:
        what = f"{instance.modpack_name} {instance.modpack_version_name or ''}".strip()
        requirements.append(f"the modpack {what}, the same version as the server (install it with your launcher)")
    elif instance.server_type in (ServerType.FORGE, ServerType.NEOFORGE, ServerType.FABRIC, ServerType.QUILT):
        requirements.append(f"{instance.server_type.title()} and the same mods as the server")
    return Guide(
        steps=[
            "Start Minecraft (Java Edition) and choose Multiplayer.",
            f"Click Add Server, enter {address} as the server address and click Done.",
            "Select the server in the list and click Join Server.",
        ],
        requirements=requirements,
        protection="whitelist",
        open_warning="Anyone with a Minecraft account who knows the address can join while the whitelist is off.",
    )


def _factorio(instance: Instance, host: str, port: int) -> Guide:
    version = str(instance.minecraft_version or "")
    requirements = [f"Factorio {version}" if version else "Factorio, the same version as the server"]
    if (instance.game_options or {}).get("space_age"):
        requirements.append("the Space Age expansion")
    return Guide(
        steps=[
            "Start Factorio and choose Multiplayer, then Connect to address.",  # general knowledge
            f"Enter {_hostport(host, port, 34197)} and click Connect.",
            "Enter the game password when Factorio asks for it.",
        ],
        requirements=requirements,
        open_warning="Anyone with a factorio.com account who knows the address can join while the game password is empty.",
    )


def _satisfactory(instance: Instance, host: str, port: int) -> Guide:
    return Guide(
        steps=[
            "Start Satisfactory and open the Server Manager in the main menu.",  # general knowledge
            f"Click Add Server, enter {host} as the address and {port} as the port, and confirm.",
            "Select the server and click Join Game; enter the join password when asked.",
        ],
        requirements=["Satisfactory on the same branch as the server (the normal Steam/Epic version)"],
        open_warning="Anyone who knows the address can join while the join password is off.",
    )


def _terraria(instance: Instance, host: str, port: int) -> Guide:
    from .terraria import TMODLOADER, TSHOCK

    variant = str(instance.server_type or "").lower()
    version = str(instance.minecraft_version or "")
    if variant == TMODLOADER:
        client, requirements = "tModLoader", ["tModLoader (on Steam); the server's mods are offered for download when you join"]
    else:
        client = "Terraria"
        terraria_version = version.split("-", 1)[0] if variant == TSHOCK else version
        requirements = [f"Terraria {terraria_version}" if terraria_version else "Terraria, the same version as the server"]
    steps = [
        f"Start {client}, choose Multiplayer and then Join via IP.",
        "Pick a character.",
        f"Enter {host} as the address, then {port} as the port.",
        "Enter the server password when asked.",
    ]
    if variant == TSHOCK:
        steps.append(
            "TShock may ask you to register: type /register <password> in the chat, then /login <password>."
        )  # general knowledge
    return Guide(
        steps=steps,
        requirements=requirements,
        open_warning="Anyone who knows the address can join while the server password is empty.",
    )


def _palworld(instance: Instance, host: str, port: int) -> Guide:
    return Guide(
        steps=[
            "Start Palworld and choose Join Multiplayer Game.",  # general knowledge
            f"Enter {_hostport(host, port)} in the address field at the bottom and click Connect.",
            "Enter the join password when asked.",
        ],
        requirements=["Palworld, the current version (Steam keeps it up to date, like the server)"],
        open_warning="Anyone who knows the address can join while the join password is empty.",
    )


def _valheim(instance: Instance, host: str, port: int) -> Guide:
    steps = [
        "Start Valheim, choose Start Game and pick a character.",  # general knowledge
        "Choose Join Game, click Add Server and enter " + _hostport(host, port) + ".",
        "Connect and enter the join password.",
    ]
    options = instance.game_options or {}
    server = options.get("server") if isinstance(options.get("server"), dict) else {}
    if server.get("crossplay"):
        steps.append(
            "With crossplay on, Xbox and Game Pass players use the join code instead: it is in the server's console "
            "when it starts (ask the owner for it)."
        )
    return Guide(
        steps=steps,
        requirements=["Valheim, the current version (Steam keeps it up to date, like the server)"],
        open_warning="",  # the join password is required
    )


def _zomboid(instance: Instance, host: str, port: int) -> Guide:
    branch = "Build 41" if "41" in str(instance.minecraft_version or "") else "Build 42"
    return Guide(
        steps=[
            "Start Project Zomboid and choose Join.",  # general knowledge
            f"Click Add, enter {host} as the IP and {port} as the port, and choose an account name and password.",
            "Select the server and click Join Server.",
        ],
        requirements=[f"Project Zomboid {branch}, like the server"],
        protection="accounts",
        open_warning="Anyone who knows the address can create an account while 'Anyone may create an account' is on.",
    )


def _hkmp(instance: Instance, host: str, port: int) -> Guide:
    return Guide(
        steps=[
            "Install the HKMP mod 2.4.3 with a Hollow Knight mod installer (for example Lumafly).",  # general knowledge
            "Start Hollow Knight, load a save and pause the game: the HKMP menu is at the top left.",  # general knowledge
            f"Enter {host} as the address and {port} as the port, choose a name and click Connect.",
        ],
        requirements=["Hollow Knight with the HKMP mod 2.4.3 (the server's version)"],
        protection="whitelist",
        open_warning="Anyone who knows the address can join while the HKMP whitelist is off (the default).",
    )


def _cs2(instance: Instance, host: str, port: int) -> Guide:
    return Guide(
        steps=[
            "Start Counter-Strike 2 and turn on the developer console (Settings, Game, Enable Developer Console).",  # general knowledge
            "Open the console with the ~ key.",
            f"Type: connect {_hostport(host, port)}  (with a join password: password <password>; connect {_hostport(host, port)}).",
        ],
        requirements=["Counter-Strike 2 (Steam keeps it up to date, like the server)"],
        open_warning="Anyone who knows the address can join while the join password is empty.",
    )


def _rust(instance: Instance, host: str, port: int) -> Guide:
    return Guide(
        steps=[
            "Start Rust and press F1 to open the console.",  # general knowledge
            f"Type: client.connect {_hostport(host, port)}",
        ],
        requirements=["Rust (Steam keeps it up to date, like the server)"],
        protection="none",
        open_warning="Anyone can join: Rust servers have no join password here, and they usually appear in the in-game list.",
    )


GUIDES = {
    "minecraft": _minecraft,
    "factorio": _factorio,
    "satisfactory": _satisfactory,
    "terraria": _terraria,
    "palworld": _palworld,
    "valheim": _valheim,
    "project-zomboid": _zomboid,
    "hkmp": _hkmp,
    "cs2": _cs2,
    "rust": _rust,
}


def guide(instance: Instance, host: str, port: int) -> Guide:
    make = GUIDES.get(instance.game or "minecraft")
    if make is None:
        return Guide(steps=[f"Connect to {_hostport(host, port)} with the game's client."], protection="none")
    return make(instance, host, port)


# --- the join password ---------------------------------------------------------------------------------------
# a secret of the server (Settings, Game settings, Secrets)
SECRET_PASSWORDS = {
    "valheim": "server_password",
    "palworld": "server_password",
    "cs2": "server_password",
    "satisfactory": "client_password",
}
# a setting in the game's own file: (config file id, key)
FILE_PASSWORDS = {"factorio": ("server-settings", "game_password")}


def _file_value(client: AgentClient, instance: Instance, file_id: str, key: str) -> Any:
    from apps import games
    from apps.core.errors import ApiError

    from . import codecs

    definition = games.for_instance(instance)
    cfg = definition.config_file_for(instance, file_id)
    if cfg is None:
        return None
    try:
        text = client.read_text(instance.id, cfg.path)["content"]
    except ApiError:
        return None
    try:
        values = codecs.get(cfg.codec).parse(text or "")
    except ValueError:
        return None
    if key in values:
        return values.get(key)
    node: Any = values  # (a nested JSON file: "Settings.ServerPassword")
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def join_password(instance: Instance, client: AgentClient | None) -> str | None:
    """The server's join password ("" when it has none, None when it cannot be read now)."""
    game = instance.game or "minecraft"
    if game in SECRET_PASSWORDS:
        return str(instance.secret_values.get(SECRET_PASSWORDS[game]) or "")
    if client is None:
        return None
    if game == "terraria":
        from .terraria import TSHOCK

        if str(instance.server_type or "").lower() == TSHOCK:
            value = _file_value(client, instance, "tshock", "Settings.ServerPassword")
        else:
            value = _file_value(client, instance, "serverconfig", "password")
        return None if value is None else str(value)
    if game in FILE_PASSWORDS:
        value = _file_value(client, instance, *FILE_PASSWORDS[game])
        return None if value is None else str(value)
    return ""  # the game has no join password (Minecraft, Project Zomboid, HKMP, Rust)
