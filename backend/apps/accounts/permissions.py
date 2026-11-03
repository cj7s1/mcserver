"""Permission matrix.

Permissions are plain string codes. A *Role* is a named set of codes.

* A user's global role grants its codes everywhere (system-level codes and
  instance-level codes on every instance).
* An InstanceAccess grant applies a role's *instance-level* codes to one
  instance only.
* The owner account implicitly holds every permission.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionDef:
    code: str
    label: str
    description: str
    scope: str  # "system" or "instance"
    group: str


PERMISSIONS: tuple[PermissionDef, ...] = (
    PermissionDef("system.settings", "System settings", "Change panel settings and provider credentials", "system", "System"),
    PermissionDef("system.users", "Manage users", "Create, edit and disable panel users and roles", "system", "System"),
    PermissionDef("system.audit", "View audit log", "Read the audit trail for all activity", "system", "System"),
    PermissionDef("system.nodes", "Manage nodes", "View host resources and node configuration", "system", "System"),
    PermissionDef("instance.create", "Create servers", "Create, import and clone servers", "system", "System"),
    PermissionDef("instance.view", "View server", "See status, overview and configuration", "instance", "General"),
    PermissionDef("instance.control", "Power control", "Start, stop, restart and kill the server", "instance", "General"),
    PermissionDef("instance.console", "View console", "Read live console output and logs", "instance", "Console"),
    PermissionDef("instance.command", "Send commands", "Run console commands through RCON", "instance", "Console"),
    PermissionDef(
        "instance.configure",
        "Configure server",
        "Change properties, resources, JVM, networking and software",
        "instance",
        "Configuration",
    ),
    PermissionDef("instance.files.read", "Read files", "Browse and download server files", "instance", "Files"),
    PermissionDef("instance.files.write", "Write files", "Upload, edit, rename and delete server files", "instance", "Files"),
    PermissionDef(
        "instance.content",
        "Manage mods/plugins",
        "Install, update and remove mods, plugins and modpack versions",
        "instance",
        "Content",
    ),
    PermissionDef("instance.players", "Manage players", "Whitelist, operators, bans and kicks", "instance", "Players"),
    PermissionDef("instance.backup", "Create backups", "Create backups and view backup history", "instance", "Backups"),
    PermissionDef("instance.restore", "Restore backups", "Restore or delete backups (destructive)", "instance", "Backups"),
    PermissionDef(
        "instance.schedules", "Manage schedules", "Manage scheduled restarts, backups and commands", "instance", "Automation"
    ),
    PermissionDef(
        "instance.access", "Manage access", "Grant other panel users access to this server", "instance", "Administration"
    ),
    PermissionDef("instance.delete", "Delete server", "Archive or permanently delete the server", "instance", "Administration"),
)

ALL_CODES: frozenset[str] = frozenset(p.code for p in PERMISSIONS)
INSTANCE_CODES: frozenset[str] = frozenset(p.code for p in PERMISSIONS if p.scope == "instance")
SYSTEM_CODES: frozenset[str] = frozenset(p.code for p in PERMISSIONS if p.scope == "system")

OPERATOR_CODES = {
    "instance.view",
    "instance.control",
    "instance.console",
    "instance.command",
    "instance.players",
    "instance.backup",
    "instance.files.read",
}

SYSTEM_ROLES: dict[str, dict] = {
    "administrator": {
        "name": "Administrator",
        "description": "Full control of every server, user and setting (except the owner account).",
        "permissions": sorted(ALL_CODES),
    },
    "server_manager": {
        "name": "Server manager",
        "description": "Full control of assigned servers, including configuration, content and restores.",
        "permissions": sorted(INSTANCE_CODES - {"instance.access"}),
    },
    "operator": {
        "name": "Operator",
        "description": "Day-to-day operation: power, console, players and backups.",
        "permissions": sorted(OPERATOR_CODES),
    },
    "viewer": {
        "name": "Viewer",
        "description": "Read-only access to status and console output.",
        "permissions": ["instance.console", "instance.view"],
    },
}


def validate_codes(codes: list[str]) -> list[str]:
    unknown = sorted(set(codes) - ALL_CODES)
    if unknown:
        raise ValueError(f"Unknown permission codes: {', '.join(unknown)}")
    return sorted(set(codes))
