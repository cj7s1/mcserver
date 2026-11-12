"""The Share card (B1 of the feature plan of 2026-11-12): what to tell a friend so they can join a server.

``share_card(instance, user, with_password=...)`` is the answer of ``GET /api/instances/<id>/share`` (permission
``instance.view``; the join password only for users with ``instance.configure``). The steps and requirements per
game are in ``apps.games.joining``. Nothing here changes the server; the agent is only asked for the files that
hold a join password or the whitelist switch (a node that does not answer leaves those "unknown").
"""

from __future__ import annotations

import logging
from typing import Any

from apps.core.models import SystemSetting
from apps.games import joining

from .models import Instance

log = logging.getLogger(__name__)

PROTECTION_LABELS = {
    "password": "a join password",
    "whitelist": "a whitelist",
    "accounts": "accounts created by the owner only",
    "none": "nothing",
}


def _address(instance: Instance) -> dict[str, Any]:
    panel = str(SystemSetting.get("public_address") or "").strip()
    node = str(getattr(instance.node, "public_address", "") or "").strip()
    if panel:
        return {"host": panel, "source": "panel", "note": ""}
    if node:
        return {"host": node, "source": "node", "note": ""}
    return {
        "host": None,
        "source": None,
        "note": "No public address is set: enter the address your friends use (your router's public IP or a DNS name) "
        "under Settings, System, Public address.",
    }


def _ports(instance: Instance) -> list[dict[str, Any]]:
    """The published host ports with their protocol (what the server's container is made with)."""
    from .spec import build_spec

    try:
        published = build_spec(instance).get("ports") or []
    except Exception:  # a server whose spec cannot be built (an unknown game): nothing to forward
        log.exception("spec of %s for the share card", instance.pk)
        return []
    names = {(int(p.get("host_port") or 0), str(p.get("protocol") or "")): p for p in instance.ports or [] if isinstance(p, dict)}
    out = []
    for port in published:
        number, protocol = int(port.get("host_port") or 0), str(port.get("protocol") or "tcp")
        if not number:
            continue
        named = names.get((number, protocol)) or {}
        out.append({"port": number, "protocol": protocol, "name": str(named.get("name") or ("game" if not out else ""))})
    return out


def _whitelist_on(instance: Instance, client: Any) -> bool | None:
    if (instance.game or "minecraft") != "minecraft":
        return None  # (HKMP keeps it in a file the server writes itself: not read here)
    from . import properties

    try:
        raw = properties.read_file(client, instance.id)
    except Exception:
        return None
    if raw is None:
        return False
    return str(properties.parse(raw).get("white-list", "false")).strip().lower() == "true"


def _accounts_only(instance: Instance, client: Any) -> bool | None:
    value = joining._file_value(client, instance, "server-ini", "Open")
    if value is None:
        return None
    return str(value).strip().lower() == "false"


def share_card(instance: Instance, user: Any, *, with_password: bool = False) -> dict[str, Any]:
    from apps.nodes.client import AgentClient

    may_read_password = bool(user is None or user.has_panel_perm("instance.configure", instance))
    client = AgentClient(instance.node, timeout=10)
    address = _address(instance)
    ports = _ports(instance)
    host = address["host"] or "<address>"
    game_port = ports[0]["port"] if ports else int(instance.game_port or 0)
    guide = joining.guide(instance, host, game_port)

    password: str | None = None
    if guide.protection == "password":
        try:
            password = joining.join_password(instance, client)
        except Exception:
            log.exception("join password of %s for the share card", instance.pk)
            password = None
        protected: bool | None = None if password is None else bool(password)
    elif guide.protection == "whitelist":
        protected = _whitelist_on(instance, client)
    elif guide.protection == "accounts":
        protected = _accounts_only(instance, client)
    else:
        protected = False

    show_password = with_password and may_read_password and bool(password)
    connect = f"{host}:{game_port}" if game_port else host
    lines = [f'Join my {_game_label(instance)} server "{instance.name}"!']
    lines.append(f"Address: {connect}" if address["host"] else "Address: ask me for it")
    if show_password:
        lines.append(f"Password: {password}")
    elif guide.protection == "password" and protected:
        lines.append("Password: ask me for it")
    if guide.requirements:
        lines.append("You need: " + "; ".join(guide.requirements) + ".")
    lines.append("How to join:")
    lines += [f"{n}. {step}" for n, step in enumerate(guide.steps, start=1)]

    forward = ", ".join(f"{p['protocol'].upper()} {p['port']}" for p in ports)
    checklist = [
        {
            "id": "running",
            "ok": instance.status in ("running",),
            "label": "The server is running",
            "hint": "" if instance.status == "running" else "Start it before your friends join.",
        },
        {
            "id": "public_address",
            "ok": bool(address["host"]),
            "label": "A public address is set",
            "hint": address["note"],
        },
        {
            "id": "protection",
            "ok": protected,
            "label": f"Strangers are kept out by {PROTECTION_LABELS[guide.protection]}"
            if guide.protection != "none"
            else "This game has no way to keep strangers out",
            "hint": guide.open_warning if protected is not True else "",
        },
        {
            "id": "ports",
            "ok": None,
            "label": f"Forward on your router: {forward}" if forward else "No port is published",
            "hint": "The panel does not change your router; forward these ports to this server's host."
            if forward
            else "The server publishes no port yet (Satisfactory: until its setup claimed it).",
        },
    ]
    answer: dict[str, Any] = {
        "game": instance.game or "minecraft",
        "game_label": _game_label(instance),
        "address": address,
        "connect": connect if address["host"] else None,
        "ports": ports,
        "protection": {"kind": guide.protection, "on": protected, "label": PROTECTION_LABELS[guide.protection]},
        "steps": guide.steps,
        "requirements": guide.requirements,
        "open_warning": guide.open_warning,
        "invite_text": "\n".join(lines),
        "invite_has_password": show_password,
        "checklist": checklist,
    }
    if may_read_password:
        answer["password"] = password if guide.protection == "password" else None
    return answer


def _game_label(instance: Instance) -> str:
    from apps import games

    definition = games.get_or_none(instance.game or games.DEFAULT_GAME)
    return definition.label if definition is not None else str(instance.game)
