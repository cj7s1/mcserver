"""Turn server logs and well-known files into actionable problem reports.

Each detector looks for a specific, verified failure signature (from real itzg /
Forge / Fabric output) and explains what to do. Detectors never guess: if no
signature matches, nothing is reported.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from apps import games
from apps.core.errors import ApiError
from apps.nodes.client import AgentClient, AgentError

from .models import Instance

log = logging.getLogger(__name__)


@dataclass
class Issue:
    code: str
    severity: str  # error | warning | info
    title: str
    detail: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_JAVA_CLASS_VERSION = {52: 8, 55: 11, 60: 16, 61: 17, 65: 21, 69: 25}


def _grep(lines: list[str], pattern: str, limit: int = 5) -> list[str]:
    rx = re.compile(pattern, re.I)
    return [ln[:400] for ln in lines if rx.search(ln)][:limit]


def analyze_logs(instance: Instance, lines: list[str]) -> list[Issue]:
    issues: list[Issue] = []
    text = "\n".join(lines)

    if "Missing Mods Checker" in text and "HeadlessException" in text:
        issues.append(
            Issue(
                "missing_mods_checker",
                "error",
                "The modpack requires manually downloaded files",
                "The pack's 'Missing Mods Checker' found files that must be downloaded by hand (usually CurseForge-only "
                "mods) and crashed because a server has no screen. Download the listed files into the server, or use "
                "the pack's CurseForge edition, which installs them automatically.",
                [{"kind": "manual_downloads", "label": "Show required files"}],
                _grep(lines, r"Missing Mods Checker"),
            )
        )
    if "Failed to auto-install CurseForge modpack" in text or "have disallowed project distribution" in text:
        issues.append(
            Issue(
                "curseforge_manual_downloads",
                "error",
                "Some CurseForge files must be downloaded manually",
                "The authors of some mods disallow automated downloads. Download them from the listed CurseForge pages "
                "and upload them into the server's 'downloads' folder (or exclude them), then start the server again.",
                [{"kind": "manual_downloads", "label": "Show required files"}],
                _grep(lines, r"disallowed project distribution|Failed to auto-install"),
            )
        )
    m = re.search(r"class file version (\d+)\.\d+.*?up to (\d+)\.\d+", text)
    if m:
        need = _JAVA_CLASS_VERSION.get(int(m.group(1)), int(m.group(1)) - 44)
        issues.append(
            Issue(
                "java_too_old",
                "error",
                f"A mod or the server needs Java {need}",
                f"The server runs Java {instance.java_version}, but some code was compiled for Java {need}. "
                f"Change the Java version in Settings → Software to {need} (or newer) and restart.",
                [{"kind": "settings", "label": "Change Java version", "field": "java_version", "value": need}],
                _grep(lines, r"class file version"),
            )
        )
    if "ClassLoaders$AppClassLoader cannot be cast to class java.net.URLClassLoader" in text:
        issues.append(
            Issue(
                "java_too_new",
                "error",
                "This Forge version needs Java 8",
                "Old Forge versions (1.16.5 and earlier) only run on Java 8. Set Java 8 in Settings → Software.",
                [{"kind": "settings", "label": "Use Java 8", "field": "java_version", "value": 8}],
                _grep(lines, r"URLClassLoader"),
            )
        )
    if instance.oom_killed or "Exit code 137 usually indicates" in text or "java.lang.OutOfMemoryError" in text:
        issues.append(
            Issue(
                "out_of_memory",
                "error",
                "The server ran out of memory",
                f"The server is configured with {instance.memory_mb} MB of heap and a {instance.container_memory_mb} MB "
                "container limit. Large modpacks typically need 6-10 GB. Increase memory in Settings → Resources.",
                [{"kind": "settings", "label": "Increase memory", "field": "memory_mb"}],
                _grep(lines, r"OutOfMemoryError|Exit code 137"),
            )
        )
    if "Could not reserve enough space for object heap" in text:
        issues.append(
            Issue(
                "heap_too_large",
                "error",
                "Java could not allocate the configured heap",
                "The requested memory is larger than what the host can provide. Lower the memory setting.",
                [{"kind": "settings", "label": "Lower memory", "field": "memory_mb"}],
                _grep(lines, r"reserve enough space"),
            )
        )
    deps = _grep(
        lines,
        r"Missing or unsupported mandatory dependencies|requires .* (?:version|any version)|"
        r"Mod .* requires|Incompatible mods? found|depends on .* which is missing",
        12,
    )
    if deps:
        issues.append(
            Issue(
                "missing_dependencies",
                "error",
                "Mods are missing required dependencies",
                "One or more mods need another mod (or a different version) that is not installed. "
                "Install the listed dependencies or remove the mods that need them.",
                [{"kind": "content", "label": "Open mods"}],
                deps,
            )
        )
    if re.search(r"FAILED TO BIND TO PORT|Address already in use", text, re.I):
        issues.append(
            Issue(
                "port_in_use",
                "error",
                "The game port is already in use",
                "Another program is using the configured port. Choose a different port in the Network tab.",
                [{"kind": "network", "label": "Change port"}],
                _grep(lines, r"BIND TO PORT|Address already in use"),
            )
        )
    mixin = _grep(lines, r"Mixin apply (?:for mod )?\S* failed|MixinApplyError|InvalidMixinException", 5)
    if mixin:
        issues.append(
            Issue(
                "mixin_failure",
                "error",
                "A mod failed to patch the game (mixin error)",
                "This usually means a mod is incompatible with the loader, Minecraft version or another mod. "
                "The log lines below name the mod; update or remove it.",
                [{"kind": "content", "label": "Open mods"}],
                mixin,
            )
        )
    client_only = _grep(
        lines,
        r"(?:Attempted to load class net/minecraft/client|Environment type CLIENT|"
        r"is a client-side mod|net\.minecraft\.client\.\w+ (?:for invalid dist|not found))",
        5,
    )
    if client_only:
        issues.append(
            Issue(
                "client_only_mod",
                "error",
                "A client-only mod is installed on the server",
                "Some mods only work in the game client and crash dedicated servers. Disable or remove the mod named in "
                "the log lines below.",
                [{"kind": "content", "label": "Open mods"}],
                client_only,
            )
        )
    if "You need to agree to the EULA" in text:
        issues.append(
            Issue(
                "eula",
                "error",
                "The Minecraft EULA was not accepted",
                "Recreate the container from Settings → Danger zone; the panel sets EULA=TRUE.",
                [],
                [],
            )
        )
    return issues


def manual_downloads(instance: Instance) -> dict[str, Any]:
    """Files a modpack expects but that could not be installed automatically."""
    client = AgentClient(instance.node)
    present: set[str] = set()
    for folder in ("mods", "datapacks", "resourcepacks", "downloads", "downloads/mods"):
        try:
            present |= {e["name"] for e in client.list_files(instance.id, folder)["entries"]}
        except ApiError:
            continue
    items: list[dict[str, Any]] = []
    sources: list[str] = []
    try:
        raw = client.read_text(instance.id, "config/missing_mods_checker.json")["content"]
        entries = json.loads(raw)
        sources.append("config/missing_mods_checker.json")
        for e in entries if isinstance(entries, list) else []:
            pattern = str(e.get("pattern", ""))
            dest = str(e.get("destination") or "mods").strip("/") or "mods"
            items.append(
                {
                    "name": str(e.get("displayName") or pattern),
                    "filename": pattern,
                    "destination": dest,
                    "url": str(e.get("url") or ""),
                    "present": pattern in present,
                    "source": "missing_mods_checker",
                }
            )
    except (ApiError, json.JSONDecodeError):
        pass
    try:
        txt = client.read_text(instance.id, "MODS_NEED_DOWNLOAD.txt")["content"]
        sources.append("MODS_NEED_DOWNLOAD.txt")
        for line in txt.splitlines():
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if len(cols) >= 4 and cols[0].lower() != "mod" and not set(cols[0]) <= {"-", "="}:
                items.append(
                    {
                        "name": cols[0],
                        "version": cols[1],
                        "filename": cols[2],
                        "destination": "downloads",
                        "url": cols[3],
                        "present": cols[2] in present,
                        "source": "curseforge",
                    }
                )
    except ApiError:
        pass
    checker_installed = any(n.startswith("missingmodschecker") and n.endswith(".jar") for n in present)
    return {
        "sources": sources,
        "items": items,
        "missing": sum(1 for i in items if not i["present"]),
        "missing_mods_checker_enabled": checker_installed,
        "notes": [
            "Download each file in your browser from the linked page (automated downloads are not permitted by the "
            "authors) and upload it to the listed folder with the Files tab or the upload button.",
            "CurseForge files go in 'downloads/': the runtime picks them up from there on the next start.",
        ],
    }


def diagnose(instance: Instance, tail: int = 1500) -> dict[str, Any]:
    """Problems found in the console output and the state of ONE run of the server.

    While the server has a container, that is the container's own run and nothing else: the
    agent keeps the end of the previous run in front of a new container's log for the console,
    and read here it would show the problem of a run that is over (a server repaired with
    "Change Java version" would keep its old diagnosis, a new crash loop would be explained with
    the previous one's cause). A stopped server has no container: then the run is the last one,
    from the record the agent kept of it (console lines and restart count), so the cause of a
    crash loop is still shown after the server was stopped.
    """
    client = AgentClient(instance.node)
    status = client.status(instance.id)
    exists = bool(status.get("exists"))
    try:
        lines = client.logs(instance.id, tail=tail, previous=not exists)
    except AgentError as exc:
        # no_container: neither a container nor a record. Anything else the agent answers here
        # (an agent from before "a container that is being removed counts as none" answers a
        # Docker error while Stop removes the container) only means: no lines to look at now.
        if exc.error_code != "no_container":
            log.info("diagnostics of %s: no console lines (%s: %s)", instance.pk, exc.error_code, exc.detail)
        lines = []
    issues = games.for_instance(instance).analyze_logs(instance, lines)  # Minecraft: analyze_logs
    run = status if exists else (status.get("last_run") or {})
    restarts = int(run.get("restart_count") or 0)
    if restarts >= 3 and instance.ready_at is None:
        issues.insert(
            0,
            Issue(
                "crash_loop",
                "error",
                f"The server keeps crashing ({restarts} restarts)",
                "The server exits during startup and Docker restarts it. The problems below (if any) explain why; "
                "otherwise check the console log for the first error.",
                [{"kind": "console", "label": "Open console"}],
                [],
            ),
        )
    return {"issues": [i.as_dict() for i in issues], "restart_count": restarts, "lines_scanned": len(lines)}
