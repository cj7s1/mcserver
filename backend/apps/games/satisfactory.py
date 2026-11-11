"""Satisfactory (dedicated server on wolveix/satisfactory-server): the third game definition.

What the panel knows about it (docs/design/GAME-DEFINITIONS.md 4.5 and 8.5, corrected by the spike of
2026-11-10, audit-2026-11-09/design/spike-results.md section 5):

* Image ``wolveix/satisfactory-server:<pinned tag>`` (the panel release pins the image; the game itself
  is downloaded by SteamCMD, about 2.9 GB, into ``gamefiles/`` and re-validated at every start). Clients
  and servers must run the same build, so the server follows Steam's stable branch: there is no version
  to pick and no software update; "update" means a restart.
* Root mode on Unraid (``PUID``/``PGID`` = the runtime uid/gid, CHOWN DAC_OVERRIDE SETUID SETGID; the
  agent's ``satisfactory`` profile). Memory: plain container limit, 12 GB by default, 8 GB at least.
* Three named ports: the game on UDP and the HTTPS API on TCP share one host port number (default
  7777; the container port stays 7777 because the claim is stored per game port), and the reliable
  messaging port (default 8888/tcp) is advertised to players as it is set, so it is set to the host port
  and published 1:1.
* **Claiming** (setup steps): the install starts the server with no port published; the ``setup`` job
  then, through the agent on the internal network, claims it (``PasswordlessLogin`` -> ``ClaimServer``
  with the server's control secret as admin password), stores a fresh API token encrypted
  (``Instance.secrets["api_token"]``, written to ``.mcpanel/api-token`` for the agent before every
  apply), sets the client password the owner chose or confirms the explicit "open" choice, creates the
  game and only then recreates the container with its ports published (``setup_state.ports_open``).
  The agent refuses to publish a server that still accepts an InitialAdmin login.
* Control through the API only: no console, no broadcast, no player names (a count from
  ``QueryServerState``); saves with ``SaveGame``; stop = ``SaveGame`` + ``Shutdown``. The restart
  policy is "no": only the agent starts the container, through its start wrapper, which refuses a
  published start unless the agent verified the claim (the claim marker; review of 2026-11-11). The
  install job and the Start job retry SteamCMD's failures themselves.
* Saves (``saved/server/*.sav``): list (``EnumerateSessions`` while running), new game
  (``CreateNewGame``), import (``UploadSaveGame``) and export (``SaveGame``, then the file).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from apps.core.errors import ApiError, ConflictError

from .base import ConfigFile, GameDefinition, GameOption, PortSpec, SecretSpec, SetupStep, SetupWaiting

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.jobs.models import Job
    from apps.jobs.runner import JobContext
    from apps.nodes.client import AgentClient

log = logging.getLogger(__name__)

GAME = "satisfactory"
DEFAULT_REPOSITORY = "wolveix/satisfactory-server"
# the image the panel release runs (exact tag; verified on real containers, see TESTING.md)
IMAGE_TAG = "v1.9.11"
BRANCH = "stable"
GAME_PORT = 7777
MESSAGING_PORT = 8888
TOKEN_FILE = ".mcpanel/api-token"
# the claim (admin password, API tokens, the server's certificate) of the game port the container
# uses: always 7777 inside the container (ARCHITECTURE.md "Satisfactory")
CLAIM_FILE = f"saved/ServerSettings.{GAME_PORT}.sav"
CLAIM_DIR = "saved"
TOKEN_SECRET = "api_token"
CLIENT_PASSWORD = "client_password"
SAVES_DIR = "saved/server"
SAVE_SUFFIX = ".sav"
API_WAIT = 30 * 60  # seconds the claim waits for the first start (SteamCMD download) to finish
SESSION_RE = re.compile(r"^[A-Za-z0-9 _-]{1,64}$")
SAVE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# names the game (``<session>_autosave_<n>``) or the panel (``<session>_mcpanel``, the agent's SaveGame)
# write to later: an imported save never gets one
RESERVED_SAVE_NAME = re.compile(r"(_autosave_\d+|_mcpanel)$", re.IGNORECASE)
SERVER_NAME_CHARS = re.compile(r"[^A-Za-z0-9 _.'-]+")

ACCESS_OPTION = GameOption(
    "access",
    "Who may join",
    "enum",
    None,
    "Satisfactory lets anyone who reaches the server log in as a client until a client password is set. "
    "Choose explicitly: a join password (enter it on the last step), or open to anyone who knows the address.",
    pinned=False,
    options=("password", "open"),
    option_labels=(("password", "A join password"), ("open", "Open to anyone who knows the address")),
)

# Launch settings: the image turns these variables into the server's ini overrides at every start.
LAUNCH_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "max_players",
        "type": "int",
        "label": "Max players",
        "help": "More than 4 players needs more memory (16 GB recommended for big factories).",
        "min": 1,
        "max": 127,
        "default": 4,
        "initial": True,
    },
    {"key": "autosave_slots", "type": "int", "label": "Rotating autosaves", "min": 1, "max": 100, "default": 5},
    {"key": "max_tick_rate", "type": "int", "label": "Max tick rate", "min": 10, "max": 120, "default": 30},
    {"key": "timeout", "type": "int", "label": "Connection timeout (seconds)", "min": 5, "max": 300, "default": 30},
    {"key": "server_streaming", "type": "bool", "label": "Server streaming", "default": True},
    {"key": "seasonal_events", "type": "bool", "label": "Seasonal events (FICSMAS)", "default": True},
)
LAUNCH_ENV = {
    "max_players": "MAXPLAYERS",
    "autosave_slots": "AUTOSAVENUM",
    "max_tick_rate": "MAXTICKRATE",
    "timeout": "TIMEOUT",
    "server_streaming": "SERVERSTREAMING",
}

# Server options through the API (GetServerOptions / ApplyServerOptions; keys and values as the spike
# read them from build 517554: strings, "True"/"False", the autosave interval in seconds).
OPTION_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "FG.AutosaveInterval",
        "type": "int",
        "label": "Autosave every (seconds)",
        "min": 60,
        "max": 7200,
        "default": 300,
        "restart": False,
    },
    {"key": "FG.DSAutoPause", "type": "bool", "label": "Pause while nobody is online", "default": True, "restart": False},
    {
        "key": "FG.DSAutoSaveOnDisconnect",
        "type": "bool",
        "label": "Save when a player leaves",
        "default": True,
        "restart": False,
    },
    {
        "key": "FG.NetworkQuality",
        "type": "enum",
        "label": "Network quality (0 low to 3 ultra)",
        "options": ["0", "1", "2", "3"],
        "default": "1",
        "restart": False,
    },
    {
        "key": "FG.SendGameplayData",
        "type": "bool",
        "label": "Send gameplay data to Coffee Stain",
        "default": False,
        "restart": False,
    },
)

CONFIG_FILES = (
    ConfigFile(
        id="launch",
        path="(container settings)",
        codec="json",
        label="Launch settings",
        help="Kept by the panel and passed to the server at its start (restart to apply).",
        fields=LAUNCH_FIELDS,
    ),
    ConfigFile(
        id="server-options",
        path="(server API)",
        codec="json",
        label="Server options",
        help="Read from and written to the running server through its API; they apply at once. Shown while the server runs.",
        fields=OPTION_FIELDS,
    ),
)
VIRTUAL_CONFIGS = frozenset({"launch", "server-options"})

# the start wrapper's exit code when it refused a published start without a verified claim (agent:
# claimmarker.REFUSED_EXIT)
START_REFUSED_EXIT = 78
CLAIM_MARKER_FEATURE = "claim_marker"

SETUP_STEPS = (
    SetupStep(
        "claim",
        "Claim the server",
        "Whoever reaches an unclaimed Satisfactory server first owns it, so the panel claims it through its API "
        "on the internal network before any port is published. The admin password is this server's control "
        "secret; the panel keeps an API token for itself (encrypted).",
    ),
    SetupStep(
        "access",
        "Join password",
        "Sets the client password you chose, or confirms that the server is open to anyone who knows its address.",
    ),
    SetupStep(
        "new_game",
        "Load or create the game",
        "Keeps the game the server plays; without one it loads the newest save of the server's session (or the "
        "newest save there is), and creates a new game only when there is no save at all.",
    ),
    SetupStep(
        "publish",
        "Publish the ports",
        "Recreates the container with the game, API and messaging ports published. Until then nobody outside the "
        "panel can reach the server.",
    ),
)

SESSION_FIELD = {
    "key": "session_name",
    "type": "string",
    "label": "Session name",
    "help": "The name of the new game (letters, digits, spaces, _ and -). Empty: the server's name and the date.",
    "nullable": True,
}


# the steps that run again when the claim may be lost (mark_unclaimed). new_game too: without the
# settings file (its autoLoadSessionName) the server starts with no game loaded (state 1); the step
# keeps a loaded game, else loads the newest save, else creates one
RECLAIM_STEPS = ("claim", "access", "new_game", "publish")


def _touches_claim(path: str) -> bool:
    """``path`` (normalised, relative to the server folder) is ``saved/``, inside it, or a folder above it."""
    return not path or path == CLAIM_DIR or path.startswith(CLAIM_DIR + "/")


def guard_stop_of(container: dict[str, Any] | None) -> dict[str, Any] | None:
    """The agent's record that it stopped this server's container (or its last run) itself."""
    c = container or {}
    guard = c.get("guard_stop") or (c.get("last_run") or {}).get("guard_stop")
    return guard if isinstance(guard, dict) else None


def start_refused(container: dict[str, Any] | None) -> bool:
    """The container (or its last run) ended with the start wrapper's refusal (exit 78, only for a
    container with published ports): it was started outside the panel without a verified claim."""
    c = container or {}
    if c.get("running") or c.get("restarting"):
        return False
    run = c if c.get("exists", True) and "exit_code" in c else (c.get("last_run") or {})
    # (an exited container lists no ports; the wrapper exits 78 only for a published one)
    return run.get("exit_code") == START_REFUSED_EXIT


def forget_claim_marker(instance: Instance) -> None:
    """Ask the agent to remove the claim marker (best effort: the agent removes it itself on every
    restore and every change under ``saved/``, and the start wrapper compares it with the file)."""
    from apps.nodes.client import AgentClient

    try:
        client = AgentClient(instance.node)
        if client.supports(CLAIM_MARKER_FEATURE):
            client.forget_claim(instance.id)
    except ApiError as exc:
        log.warning("Could not remove the claim marker of %s: %s", instance.pk, exc.detail)


def game_loaded(state: dict[str, Any]) -> bool:
    """``QueryServerState`` describes a loaded game (``isGameRunning``; without one the server still
    names a default ``SERVER-SESSION...`` session, and ``SaveGame`` would not answer)."""
    if not isinstance(state, dict):
        return False
    running = state.get("isGameRunning")
    if running is not None:
        return bool(running)
    return bool(state.get("activeSessionName"))


def newest_save(sessions: list[dict[str, Any]], remembered: str) -> dict[str, Any] | None:
    """The newest save (by the game's ``saveDateTime``, ``YYYY.MM.DD-HH.MM.SS``) of the session the panel
    remembers, else the newest save of any session; None without saves. Entries as the agent's
    ``sessions`` action lists them (``{"name", "saves": [{"name", "session", "saved_at"}]}``)."""

    def saves_of(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for entry in entries:
            for save in entry.get("saves") or []:
                if save.get("name"):
                    out.append({**save, "session": save.get("session") or entry.get("name")})
        return out

    every = saves_of(sessions)
    preferred = [s for s in every if remembered and s.get("session") == remembered]
    pool = preferred or every
    if not pool:
        return None
    return max(pool, key=lambda s: str(s.get("saved_at") or ""))


def server_name_for(instance: Instance) -> str:
    """The name the server is claimed with (the API accepts letters, digits and a few signs here)."""
    name = " ".join(SERVER_NAME_CHARS.sub(" ", instance.name or "").split())[:64].strip()
    return name or "MCPanel server"


def session_name_for(instance: Instance, when: datetime | None = None) -> str:
    base = " ".join(re.sub(r"[^A-Za-z0-9 _-]+", " ", instance.name or "").split())[:40].strip() or "World"
    stamp = (when or datetime.now(UTC)).strftime("%Y%m%d-%H%M")
    return f"{base} {stamp}"[:64]


class SatisfactoryDefinition(GameDefinition):
    id = GAME
    label = "Satisfactory"
    profile = GAME
    control = "satisfactory-api"
    memory_model = "plain"
    capability_set = frozenset({"players", "query", "consistent_backups", "world_reset", "world_import_export", "config_files"})
    stop_words = frozenset()
    flush_command = "SaveGame"  # what the job log names as the save before a stop (an API call)
    notes = (
        "Dedicated server on the wolveix/satisfactory-server image; the game (about 2.9 GB) is downloaded on the "
        "first start and checked for updates at every start, so players' clients and the server stay on Steam's "
        "stable build. 'Update' means restart.",
        "Needs at least 8 GB of memory (12 GB by default, 16 GB for big factories or more than 4 players).",
        "The panel claims the server before publishing its ports and asks you to choose a join password or an open "
        "server. Forward the game port on UDP and TCP, and the messaging port on TCP.",
    )
    ports = (
        PortSpec(
            "game",
            "Game",
            GAME_PORT,
            "udp",
            GAME_PORT,
            "Players connect to this port (UDP). The server's API uses the same number on TCP.",
        ),
        PortSpec(
            "api",
            "Server API",
            GAME_PORT,
            "tcp",
            GAME_PORT,
            "The HTTPS API the game client's Server Manager talks to: always the same number as the game port.",
            same_port_as="game",
        ),
        PortSpec(
            "messaging",
            "Reliable messaging",
            MESSAGING_PORT,
            "tcp",
            MESSAGING_PORT,
            "TCP port the server advertises to players for reliable messages (Satisfactory's default is 8888).",
            avoid=frozenset({GAME_PORT}),  # published 1:1: 7777/tcp inside the container is the API
        ),
    )
    options = (ACCESS_OPTION,)
    secret_specs = (
        SecretSpec(
            CLIENT_PASSWORD,
            "Join password (client password)",
            "Players enter it in the game to join. Clearing it later opens the server to anyone who knows its address.",
            required=False,
            required_when=("access", "password"),
            clear_opens=True,
        ),
    )
    setup_steps = SETUP_STEPS
    min_memory_mb = 8192
    default_stop_timeout = 120
    control_secret_file = TOKEN_FILE
    custom_network = False
    retryable_install_exits = frozenset({5, 8})
    # the start wrapper's refusal of a published start without a verified claim (the Start job handles it)
    start_refused_exit = START_REFUSED_EXIT
    install_attempts = 4
    allowed_user_env = frozenset({"TZ"})
    fixed_version = {
        "id": BRANCH,
        "label": "Steam stable branch",
        "help": "Players' game clients update themselves, so the server always runs the current stable build: "
        "it checks for an update at every start. There is no version to choose.",
    }

    def __init__(self) -> None:
        self.saves = SatisfactorySaves(self)

    # -- data ------------------------------------------------------------------------------------
    def default_memory_mb(self) -> int:
        return 12288

    def config_files(self) -> tuple[ConfigFile, ...]:
        return CONFIG_FILES

    def variant(self, instance: Instance) -> str:
        return ""

    def image(self, instance: Instance) -> str:
        return f"{self.runtime_repository(DEFAULT_REPOSITORY)}:{IMAGE_TAG}"

    # -- creating a server -----------------------------------------------------------------------
    def create_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        from rest_framework.exceptions import ValidationError

        # (the create serializer validated the values that were sent; a missing choice is None here)
        options = {**self.default_options(), **(data.get("options") or {})}
        if options.get("access") not in ("password", "open"):
            raise ValidationError({"options": ["Choose who may join: a join password, or open to anyone who knows the address"]})
        secrets = data.get("secrets") or {}
        if options["access"] == "password" and not str(secrets.get(CLIENT_PASSWORD) or "").strip():
            raise ValidationError({f"secrets.{CLIENT_PASSWORD}": ["Enter the join password, or choose an open server"]})
        if options["access"] == "open" and str(secrets.get(CLIENT_PASSWORD) or "").strip():
            options["access"] = "password"  # a password given is a password chosen
        settings = data.get("settings") or {}
        launch = self._launch_defaults()
        launch.update(settings.get("launch") or {})
        options["launch"] = launch
        return {
            "server_type": "",
            "minecraft_version": BRANCH,
            "loader_version": "",
            "game_options": options,
            "memory_mb": int(data.get("memory_mb") or self.default_memory_mb()),
            "memory_overhead_mb": 0,
            "stop_timeout": self.default_stop_timeout,
            "restart_policy": "no",
            "jvm_flags_preset": "none",
            "jvm_args": "",
        }

    @staticmethod
    def _launch_defaults() -> dict[str, Any]:
        return {f["key"]: f["default"] for f in LAUNCH_FIELDS}

    def launch_values(self, instance: Instance) -> dict[str, Any]:
        values = self._launch_defaults()
        stored = (instance.game_options or {}).get("launch")
        if isinstance(stored, dict):
            values.update({k: v for k, v in stored.items() if k in values})
        return values

    # -- container -------------------------------------------------------------------------------
    def ports_open(self, instance: Instance) -> bool:
        """Whether the setup published the ports (claim, join password and game done)."""
        return bool((instance.setup_state or {}).get("ports_open"))

    def must_apply_before_start(self, instance: Instance) -> bool:
        """A container made while the ports were published is recreated (unpublished) before any start
        once the claim must be checked again (``mark_unclaimed``)."""
        return not self.ports_open(instance)

    def mark_unclaimed(self, instance: Instance, reason: str) -> bool:
        """The claim may be lost (a restore, a change under ``saved/``, the agent found the server
        unclaimed, the claim step runs again): close the ports (the next apply recreates the container
        unpublished) and make the claim, the join password and the publishing run again. Returns
        whether the ports were open (the spec changed)."""
        from django.utils import timezone

        from apps.instances.models import Instance as InstanceModel

        instance.refresh_from_db(fields=["setup_state"])
        state = dict(instance.setup_state or {})
        steps = dict(state.get("steps") or {})
        was_open = bool(state.get("ports_open"))
        now = timezone.now().isoformat()
        for step_id in RECLAIM_STEPS:
            steps[step_id] = {"status": "pending", "at": now, "message": reason[:500]}
        state["steps"] = steps
        state["ports_open"] = False
        if was_open:
            state["closed_reason"] = reason[:500]
        instance.setup_state = state
        InstanceModel.objects.filter(pk=instance.pk).update(setup_state=state)
        if was_open:
            log.warning("Satisfactory server %s: ports closed until the claim is checked again (%s)", instance.pk, reason)
        forget_claim_marker(instance)
        return was_open

    def copied_state(self, source: Instance) -> dict[str, Any]:
        """A clone or a server restored as new has the source's claim in its copied ``saved/``: it keeps
        the admin password (the control secret) and the secrets, and runs the whole setup again
        (claim = log in with that password and make its own API token; nothing is published before)."""
        return {"rcon_password": source.rcon_password, "secrets": source.secrets, "setup_state": {}}

    def world_folders(self) -> tuple[str, ...]:
        # saved/server: the sessions; backups/: the image's copy of them taken at every start
        return (SAVES_DIR, "backups")

    def secret_paths(self, instance: Instance) -> tuple[str, ...]:
        # the API token, and the claim itself (admin password, tokens, the server's certificate)
        return (TOKEN_FILE, CLAIM_FILE)

    def files_touched(self, instance: Instance, paths: list[str], why: str) -> None:
        """A change under ``saved/`` (or of the whole folder) may have lost or replaced the claim."""
        if any(_touches_claim(p) for p in paths):
            self.mark_unclaimed(instance, f"{why[:1].upper()}{why[1:]} under {CLAIM_DIR}/; the claim is checked again")

    def observed(self, instance: Instance, container: dict[str, Any] | None) -> None:
        """The agent stopped the server because it answered as unclaimed (``guard_stop``), or the start
        wrapper refused a published start without a verified claim (exit 78, e.g. a hand ``docker
        start``): close the ports, so the next start verifies the claim on an unpublished container."""
        guard = guard_stop_of(container)
        if guard and self.ports_open(instance):
            self.mark_unclaimed(instance, str(guard.get("message") or "The node agent found the server unclaimed"))
        elif start_refused(container) and self.ports_open(instance):
            self.mark_unclaimed(
                instance,
                "The server's start guard refused a start with published ports because its claim was not verified "
                "(it was started outside the panel); the claim is verified again before the ports are opened",
            )

    def game_env(self, instance: Instance) -> dict[str, str]:
        launch = self.launch_values(instance)
        env = {
            var: str(launch[key]).lower() if isinstance(launch[key], bool) else str(launch[key])
            for key, var in LAUNCH_ENV.items()
        }
        env["DISABLESEASONALEVENTS"] = "false" if launch.get("seasonal_events", True) else "true"
        env["SERVERMESSAGINGPORT"] = str(self._messaging_port(instance))
        return env

    def _messaging_port(self, instance: Instance) -> int:
        for p in instance.ports or []:
            if isinstance(p, dict) and p.get("name") == "messaging":
                return int(p["host_port"])
        return MESSAGING_PORT

    def spec_ports(self, instance: Instance) -> list[dict[str, Any]]:
        """No port before the setup published them (an unclaimed server must not be reachable); the
        messaging port 1:1 (container port = host port: the server advertises it)."""
        if not self.ports_open(instance):
            return []
        out = super().spec_ports(instance)
        messaging = self._messaging_port(instance)
        if messaging == GAME_PORT or any(
            p.get("protocol") == "tcp" and int(p.get("host_port") or 0) == messaging and p.get("name") != "messaging"
            for p in instance.ports or []
            if isinstance(p, dict)
        ):
            raise ConflictError(
                f"The messaging port {messaging}/tcp collides with the server's API port (7777/tcp in the container, "
                "the game port's number on the host); choose another messaging port",
                code="port_conflict",
            )
        for entry, spec in zip(out, self.ports, strict=True):
            if spec.name == "messaging":
                entry["container_port"] = messaging
        return out

    def build_spec(self, instance: Instance) -> dict[str, Any]:
        if instance.network:
            raise ConflictError(
                "Satisfactory servers run on the panel's instance network only; clear the server's Docker network",
                code="network_not_allowed",
            )
        spec = super().build_spec(instance)
        # only the agent starts it (through the start wrapper's claim check): Docker never does, not
        # after an exit (Shutdown exits 0, SteamCMD 5/8: the panel's jobs retry) nor after a daemon restart
        spec["restart_policy"] = "no"
        return spec

    def control_secret_value(self, instance: Instance) -> str | None:
        """The API token (not the admin password): the agent's driver authenticates with it."""
        return instance.secret_values.get(TOKEN_SECRET) or None

    # -- setup -----------------------------------------------------------------------------------
    def setup_status(self, instance: Instance) -> dict[str, Any] | None:
        status = super().setup_status(instance)
        if status is not None:
            # published = the spec has the ports and the publish step finished (while it recreates the
            # container the spec has them already, but nothing is published yet)
            publish = next((s for s in status["steps"] if s["id"] == "publish"), {})
            status["ports_open"] = self.ports_open(instance) and publish.get("status") == "done"
        return status

    def run_setup_step(self, instance: Instance, step: SetupStep, ctx: Any) -> str:
        from apps.nodes.client import AgentClient

        client = AgentClient(instance.node)
        status = client.status(instance.id)
        if not status.get("running"):
            if step.id == "publish":
                self._open_ports(instance)
                return "The ports are published when the server starts next"
            raise SetupWaiting("The server is not running; setup goes on when it is started")
        return getattr(self, f"_setup_{step.id}")(instance, ctx, client)

    def _wait_ready(self, instance: Instance, ctx: Any, client: AgentClient, timeout: float = API_WAIT) -> None:
        """Until the agent's readiness probe (the UDP query) says the server answers."""
        import time

        deadline = time.monotonic() + timeout
        while True:
            status = client.status(instance.id)
            if not status.get("running"):
                raise SetupWaiting("The server stopped; setup goes on when it is started")
            if status.get("health") == "healthy":
                return
            if time.monotonic() >= deadline:
                raise ConflictError("The server did not become ready in time; see its console", code="not_ready")
            ctx.progress(0.05, "Waiting for the server to finish its first start (the game download)")
            ctx.sleep_until(time.monotonic() + 3)

    def _setup_claim(self, instance: Instance, ctx: Any, client: AgentClient) -> str:
        from apps.instances.handlers import _recreate, write_control_secret
        from apps.instances.spec import rcon_password

        if self.ports_open(instance) and self.mark_unclaimed(
            instance, "The claim step runs again; the ports stay closed until the setup is done"
        ):
            # a re-run on a published server: unpublished first, so the claim is never made (or lost) in public
            instance.refresh_from_db()
            ctx.progress(0.05, "Recreating the container without its ports before the claim", force=True)
            _recreate(ctx.job, instance, ctx, client, running=True, start=True, span=(0.05, 0.1))
        self._wait_ready(instance, ctx, client)
        name = server_name_for(instance)
        result = client.game_action(
            instance.id, "claim", {"server_name": name, "admin_password": rcon_password(instance)}, timeout=300
        )
        token = str(result.get("token") or "")
        if not token:
            raise ConflictError("The agent returned no API token", code="claim_failed")
        instance.refresh_from_db(fields=["secrets"])
        values = instance.secret_values
        values[TOKEN_SECRET] = token
        instance.set_secret_values(values)
        instance.save(update_fields=["secrets"])
        write_control_secret(instance, client)
        if result.get("claimed") == "existing":
            return f"Already claimed by this panel as '{name}'; a new API token was stored"
        return f"Claimed as '{name}'; the API token is stored encrypted"

    def _setup_access(self, instance: Instance, ctx: Any, client: AgentClient) -> str:
        from apps.instances.spec import rcon_password

        instance.refresh_from_db(fields=["secrets", "game_options"])
        password = instance.secret_values.get(CLIENT_PASSWORD) or ""
        access = (instance.game_options or {}).get("access")
        if not password and access != "open":
            raise ConflictError(
                "Enter the join password in Settings -> Game settings -> Secrets (or clear it for an open server)",
                code="secret_missing",
            )
        # the game writes its settings file: the agent verifies the claim again (admin password) and
        # records the file in the claim marker
        result = client.game_action(
            instance.id, "client_password", {"password": password, "admin_password": rcon_password(instance)}, timeout=90
        )
        if result.get("protected"):
            return "Players need the join password"
        return "Open: anyone who knows the address can join (your explicit choice)"

    def _setup_new_game(self, instance: Instance, ctx: Any, client: AgentClient) -> str:
        """Keep the game the server plays; without one (state 1: e.g. the settings file with its
        autoload session was lost) load the newest save of the session the panel remembers, else the
        newest save there is; create a new game only when there is no save."""
        from apps.instances.spec import rcon_password

        admin = rcon_password(instance)
        state = client.game_action(instance.id, "status", {}, timeout=60).get("state") or {}
        if game_loaded(state):
            session = str(state.get("activeSessionName") or "")
            self._remember_session(instance, session)
            return f"The server plays '{session}'"
        sessions = client.game_action(instance.id, "sessions", {}, timeout=60)
        remembered = str((instance.game_options or {}).get("session_name") or "")
        save = newest_save(sessions.get("sessions") or [], remembered)
        if save is not None:
            ctx.progress(0.5, f"No game is loaded: loading the newest save '{save['name']}'", force=True)
            result = client.game_action(
                instance.id, "load_game", {"save_name": save["name"], "admin_password": admin}, timeout=300
            )
            session = str(result.get("session") or save.get("session") or "")
            self._remember_session(instance, session)
            return f"No game was loaded: loaded the newest save '{save['name']}' (session '{session}')"
        session = session_name_for(instance)
        client.game_action(instance.id, "new_game", {"session_name": session, "admin_password": admin}, timeout=300)
        self._remember_session(instance, session)
        return f"New game '{session}'"

    def _setup_publish(self, instance: Instance, ctx: Any, client: AgentClient) -> str:
        from apps.instances.handlers import _recreate
        from apps.instances.spec import rcon_password

        if client.supports(CLAIM_MARKER_FEATURE):
            # the agent verifies the claim on this unpublished container and records it in the claim
            # marker: the start wrapper of the published container (and the agent) check it
            ctx.progress(0.85, "Verifying the claim before the ports are opened", force=True)
            client.game_action(instance.id, "verify_claim", {"admin_password": rcon_password(instance)}, timeout=180)
        self._open_ports(instance)
        instance.refresh_from_db()
        ctx.progress(0.9, "Recreating the container with its ports published (the game is saved first)", force=True)
        _recreate(ctx.job, instance, ctx, client, running=True, start=True, span=(0.9, 0.95))
        instance.refresh_from_db(fields=["setup_state", "ports"])
        if self.ports_open(instance):
            # restart policy "no": SteamCMD's failures at this start are retried here (the real-container
            # run of 2026-11-11 saw exit 8 on the published container's first start)
            from apps.instances.handlers import watch_start

            watch_start(instance, ctx, client)
            instance.refresh_from_db(fields=["setup_state", "ports"])
        if not self.ports_open(instance):
            # the agent refused: the server answered as unclaimed (handlers.unclaimed_refusal closed the
            # ports again and put the claim back); it was started unpublished
            raise ConflictError(
                "The node agent refused to publish the server because it is not claimed; the claim runs again "
                "with the next setup",
                code="server_unclaimed",
            )
        ports = ", ".join(f"{p['host_port']}/{p['protocol']}" for p in instance.ports or [])
        return f"Ports published: {ports}"

    def _open_ports(self, instance: Instance) -> None:
        from apps.instances.models import Instance as InstanceModel

        instance.refresh_from_db(fields=["setup_state"])
        state = dict(instance.setup_state or {})
        state["ports_open"] = True
        instance.setup_state = state
        InstanceModel.objects.filter(pk=instance.pk).update(setup_state=state)

    def _remember_session(self, instance: Instance, session: str | None) -> None:
        from apps.instances.models import Instance as InstanceModel

        if not session:
            return
        instance.refresh_from_db(fields=["game_options"])
        options = dict(instance.game_options or {})
        options["session_name"] = session
        instance.game_options = options
        InstanceModel.objects.filter(pk=instance.pk).update(game_options=options)

    def check_secret_change(self, instance: Instance, values: dict[str, Any], request_data: dict[str, Any]) -> None:
        """Clearing the join password opens the server to anyone who knows its address: only with an
        explicit ``"access": "open"`` in the request (the UI asks first)."""
        if CLIENT_PASSWORD not in values or values[CLIENT_PASSWORD] not in (None, ""):
            return
        if not instance.secret_values.get(CLIENT_PASSWORD) and (instance.game_options or {}).get("access") == "open":
            return  # open already
        if request_data.get("access") != "open":
            raise ApiError(
                "Clearing the join password opens the server to anyone who knows its address. Confirm that with "
                '"access": "open", or enter a new password.',
                code="confirmation_required",
            )

    def secrets_changed(self, instance: Instance, keys: list[str]) -> None:
        """A new (or cleared) join password is applied by the setup's ``access`` step again; a cleared
        one means an open server (the owner's explicit choice in Settings)."""
        if CLIENT_PASSWORD not in keys:
            return
        from apps.instances.game_handlers import record_setup
        from apps.instances.models import Instance as InstanceModel

        options = dict(instance.game_options or {})
        options["access"] = "password" if instance.secret_values.get(CLIENT_PASSWORD) else "open"
        instance.game_options = options
        InstanceModel.objects.filter(pk=instance.pk).update(game_options=options)
        steps = (instance.setup_state or {}).get("steps") or {}
        if (steps.get("access") or {}).get("status") == "done":
            record_setup(instance, "access", "pending", "The join password changed; it is applied while the server runs")

    # -- config: launch settings (panel) and server options (API) ----------------------------------
    def is_virtual_config(self, cfg: ConfigFile) -> bool:
        return cfg.file_id in VIRTUAL_CONFIGS

    def _api_ready(self, instance: Instance) -> bool:
        return instance.status in ("running", "unhealthy") and bool(instance.secret_values.get(TOKEN_SECRET))

    def virtual_config_values(self, client: AgentClient, instance: Instance, cfg: ConfigFile) -> dict[str, Any] | None:
        if cfg.file_id == "launch":
            return self.launch_values(instance)
        if not self._api_ready(instance):
            return None
        try:
            options = client.game_action(instance.id, "options", {}, timeout=30).get("options") or {}
        except ApiError:
            return None  # loading or not reachable: shown again once it answers
        return {f["key"]: _from_api(f, options.get(f["key"])) for f in OPTION_FIELDS}

    def write_virtual_config(
        self, client: AgentClient, instance: Instance, cfg: ConfigFile, values: dict[str, Any]
    ) -> dict[str, Any]:
        if cfg.file_id == "launch":
            from apps.instances.models import Instance as InstanceModel
            from apps.instances.services import bump_revision

            options = dict(instance.game_options or {})
            launch = self.launch_values(instance)
            launch.update(values)
            options["launch"] = launch
            instance.game_options = options
            InstanceModel.objects.filter(pk=instance.pk).update(game_options=options)
            bump_revision(instance)
            return launch
        changes = {key: _to_api(cfg.field(key) or {}, value) for key, value in values.items()}
        answer = client.game_action(instance.id, "apply_options", {"options": changes}, timeout=60)
        options = answer.get("options") or {}
        return {f["key"]: _from_api(f, options.get(f["key"])) for f in OPTION_FIELDS}

    # -- players ---------------------------------------------------------------------------------
    def online_players(self, instance: Instance) -> dict[str, Any]:
        """A count only: the API has no player names."""
        from apps.nodes.client import AgentClient

        answer = AgentClient(instance.node).ping(instance.id)
        return {"online": answer.get("online"), "max": answer.get("max"), "players": []}

    def player_lists(self, instance: Instance) -> dict[str, Any]:
        self.require(instance, "players")
        from apps.nodes.client import AgentClient

        running = bool(AgentClient(instance.node).status(instance.id).get("running"))
        result: dict[str, Any] = {
            "running": running,
            "count_only": True,
            "labels": {},
            "whitelist": [],
            "ops": [],
            "bans": [],
            "ip_bans": [],
            "known_players": [],
        }
        if running:
            try:
                result["online"] = self.online_players(instance)
            except ApiError as exc:
                result["online"] = {"error": str(exc.detail)}
        return result

    def consistency_note(self, instance: Instance) -> str:
        return (
            "the game was saved through its API right before the copy (SaveGame answered once the file was written); "
            "the game is not paused, so an autosave during the copy can be in the backup as well"
        )

    # -- software (no update: the server follows the stable branch) ---------------------------------
    software_keys = ("game",)  # nothing a restore could set back

    def software_state(self, instance: Instance) -> dict[str, Any]:
        return {"game": self.id, "branch": BRANCH, "image": instance.image}

    def describe_software(self, state: dict[str, Any]) -> str:
        return f"Satisfactory ({state.get('branch') or BRANCH} branch)"

    def set_software(self, instance: Instance, state: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        return {}


def _from_api(field: dict[str, Any], raw: Any) -> Any:
    """A server option string as the form's typed value."""
    if raw is None:
        return field.get("default")
    if field["type"] == "bool":
        return str(raw).lower() == "true"
    if field["type"] == "int":
        try:
            return int(float(str(raw)))
        except ValueError:
            return field.get("default")
    return str(raw)


def _to_api(field: dict[str, Any], value: Any) -> str:
    if field.get("type") == "bool":
        return "True" if value else "False"
    if field.get("type") == "int":
        return f"{float(value):.1f}"
    return str(value)


class SatisfactorySaves:
    """The save files of a Satisfactory server: ``saved/server/*.sav``, grouped in sessions by the game.

    A new game (reset) and an import go through the running server's API (``CreateNewGame``,
    ``UploadSaveGame``), after a safety backup; the old saves stay where they are (other sessions).
    A stopped server is started for it and stopped again afterwards. Export saves first (``SaveGame``)
    and hands out that file.
    """

    kind = "saves"
    extension = SAVE_SUFFIX
    content_type = "application/octet-stream"

    def __init__(self, definition: SatisfactoryDefinition) -> None:
        self.definition = definition

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "extension": self.extension,
            "folder": SAVES_DIR,
            "set_aside_folder": "",
            "reset_fields": [dict(SESSION_FIELD)],
            "via_api": True,
        }

    def reset_config(self, instance: Instance) -> ConfigFile:
        return ConfigFile(id="new-game", path="", codec="json", label="New game", fields=(SESSION_FIELD,))

    def validate_upload(self, upload: Any) -> str | None:
        name = str(getattr(upload, "name", "") or "")
        if not name.lower().endswith(SAVE_SUFFIX):
            return "Upload a Satisfactory .sav save file"
        if not getattr(upload, "size", 0):
            return "The file is empty"
        return None

    def current(self, instance: Instance) -> str:
        return str((instance.game_options or {}).get("session_name") or "")

    def current_path(self, instance: Instance) -> str:
        return f"{SAVES_DIR}/"

    def _files(self, client: AgentClient, instance: Instance) -> list[dict[str, Any]]:
        try:
            entries = client.list_files(instance.id, SAVES_DIR).get("entries") or []
        except ApiError as exc:
            if exc.error_code in ("not_found", "not_a_directory"):
                return []
            raise
        return [e for e in entries if e.get("type") == "file" and str(e.get("name") or "").endswith(SAVE_SUFFIX)]

    def list(self, client: AgentClient, instance: Instance) -> dict[str, Any]:
        files = {str(e["name"])[: -len(SAVE_SUFFIX)]: e for e in self._files(client, instance)}
        session_of: dict[str, str] = {}
        current = self.current(instance)
        live = False
        if self.definition._api_ready(instance):
            try:
                answer = client.game_action(instance.id, "sessions", {}, timeout=30)
                for session in answer.get("sessions") or []:
                    for save in session.get("saves") or []:
                        session_of[str(save.get("name"))] = str(session.get("name") or "")
                # what the server plays now (none while no game is loaded), not the remembered session
                current = str(answer.get("current") or "")
                live = True
            except ApiError:
                pass  # loading: the files alone
        newest_current = max(
            (n for n in files if session_of.get(n) == current), key=lambda n: files[n].get("modified") or 0, default=None
        )
        saves = [
            {
                "name": name,
                "path": f"{SAVES_DIR}/{name}{SAVE_SUFFIX}",
                "size": entry.get("size"),
                "modified": entry.get("modified"),
                "session": session_of.get(name),
                "current": name == newest_current,
            }
            for name, entry in files.items()
        ]
        saves.sort(key=lambda s: (s["session"] != current, -(s["modified"] or 0)))
        return {
            "current": current,
            "saves": saves,
            "set_aside": [],
            "sessions": sorted({s for s in session_of.values() if s}),
            "from_api": live,
            "options": {"access": (instance.game_options or {}).get("access")},
            "reset_fields": self.describe()["reset_fields"],
        }

    def export(self, client: AgentClient, instance: Instance, *, save_first: bool) -> tuple[str, str, bool]:
        """(path, download name, saved): a running server saves first (``SaveGame``) and that file is
        handed out; otherwise the newest save file."""
        if save_first and self.definition._api_ready(instance):
            try:
                name = client.game_action(instance.id, "save", {}, timeout=120).get("save_name")
            except ApiError:
                name = None
            if name:
                return f"{SAVES_DIR}/{name}{SAVE_SUFFIX}", f"{instance.name}-{name}{SAVE_SUFFIX}", True
        files = self._files(client, instance)
        if not files:
            raise ApiError("The server has no save yet", code="not_found", status_code=404)
        newest = max(files, key=lambda e: e.get("modified") or 0)
        return f"{SAVES_DIR}/{newest['name']}", f"{instance.name}-{newest['name']}", False

    # -- the jobs -------------------------------------------------------------------------------
    def _running_for_job(self, job: Job, ctx: JobContext, instance: Instance, client: AgentClient, was_running: bool) -> None:
        """Make sure the server runs (start it when it is stopped) and its API answers."""
        from apps.instances.handlers import start_server

        if not was_running:
            ctx.progress(0.03, "Starting the server for this (it is stopped again afterwards)", force=True)
            start_server(instance, ctx, span=(0.03, 0.1))
        try:
            self.definition._wait_ready(instance, ctx, client, timeout=15 * 60)
        except SetupWaiting as exc:
            raise ConflictError(str(exc), code="not_running") from exc

    def _stop_again(self, ctx: JobContext, instance: Instance, client: AgentClient) -> None:
        from apps.instances.handlers import _log_stop, _set_desired
        from apps.instances.models import DesiredState

        ctx.progress(0.95, "Stopping the server again", force=True)
        _set_desired(instance, DesiredState.STOPPED)
        _log_stop(ctx, client.stop(instance.id, timeout=instance.stop_timeout))

    def _safety_backup(self, job: Job, ctx: JobContext, instance: Instance, what: str) -> str | None:
        from apps.backups.handlers import run_backup
        from apps.backups.models import BackupKind

        if not (job.params or {}).get("backup", True):
            ctx.log("No safety backup was requested")
            return None
        ctx.progress(0.15, "Creating a safety backup of the current state", force=True)
        safety = run_backup(
            instance,
            ctx,
            name=f"Before {what}",
            kind=BackupKind.PRE_RESTORE,
            user=job.user,
            span=(0.15, 0.6),
            job=job,
            policy_excludes=False,
        )
        if safety.warnings:
            raise ConflictError(
                "The safety backup finished with warnings (see the log of this job), so nothing was changed.",
                code="safety_backup_incomplete",
            )
        return str(safety.id)

    def run_reset(self, job: Job, ctx: JobContext) -> dict[str, Any]:
        """A new game (``CreateNewGame``) with the session name of the request; the old sessions stay."""
        return self._run(job, ctx, "starting a new game", self._new_game)

    def run_import(self, job: Job, ctx: JobContext) -> dict[str, Any]:
        """The uploaded save, uploaded to the server (``UploadSaveGame``) and loaded."""
        from apps.instances.handlers import drop_import_archive
        from apps.nodes.client import AgentClient

        try:
            return self._run(job, ctx, "importing a save", self._import)
        finally:
            instance = self._instance(job)
            drop_import_archive(AgentClient(instance.node), instance, (job.params or {}).get("path", ""))

    @staticmethod
    def _instance(job: Job) -> Instance:
        from apps.instances.models import Instance as InstanceModel

        return InstanceModel.objects.select_related("node").get(pk=job.instance_id)

    def _run(self, job: Job, ctx: JobContext, what: str, act: Any) -> dict[str, Any]:
        from apps.nodes.client import AgentClient

        instance = self._instance(job)
        if instance.archived_at:
            raise ConflictError("This server is archived; unarchive it first", code="archived")
        client = AgentClient(instance.node)
        if not instance.secret_values.get(TOKEN_SECRET):
            raise ConflictError("Finish the server's setup (claim) first", code="setup_needed")
        was_running = bool(client.status(instance.id).get("running"))
        try:
            # inside the try: a server started for this job is stopped again also when it never got ready
            self._running_for_job(job, ctx, instance, client, was_running)
            safety_id = self._safety_backup(job, ctx, instance, what)
            if safety_id is None:
                # no safety backup: the running game is saved first (SaveGame), so the session it played
                # keeps its newest state on disk before another one is created or loaded
                ctx.progress(0.6, "Saving the running game first", force=True)
                saved = client.game_action(instance.id, "save", {}, timeout=120).get("save_name")
                ctx.log(f"Saved the running game as {SAVES_DIR}/{saved}{SAVE_SUFFIX}" if saved else "No game was loaded to save")
            ctx.check_cancelled()
            ctx.progress(0.7, what.capitalize(), force=True)
            result = act(job, ctx, instance, client)
        finally:
            if not was_running:
                try:
                    self._stop_again(ctx, instance, client)
                except Exception as exc:  # the job's own result or error matters more
                    ctx.log(f"warning: the server could not be stopped again: {exc}")
        result.update(backup_id=safety_id, restarted=False, set_aside=[], folder=None)
        return result

    def _new_game(self, job: Job, ctx: JobContext, instance: Instance, client: AgentClient) -> dict[str, Any]:
        settings = (job.params or {}).get("settings") or {}
        session = str(settings.get("session_name") or "").strip() or session_name_for(instance)
        if not SESSION_RE.fullmatch(session):
            raise ApiError("Session names use letters, digits, spaces, _ and - (at most 64)", code="invalid_setting")
        existing = {
            str(s.get("name") or "") for s in client.game_action(instance.id, "sessions", {}, timeout=60).get("sessions") or []
        }
        if session in existing:
            # a new game under an existing session's name writes its autosaves over that session's
            raise ConflictError(
                f"The server already has a session '{session}'; choose another name for the new game",
                code="session_exists",
            )
        from apps.instances.spec import rcon_password

        client.game_action(
            instance.id, "new_game", {"session_name": session, "admin_password": rcon_password(instance)}, timeout=300
        )
        self.definition._remember_session(instance, session)
        ctx.log(f"New game '{session}'; the earlier sessions' saves stay in {SAVES_DIR}/")
        return {"session": session}

    def _import(self, job: Job, ctx: JobContext, instance: Instance, client: AgentClient) -> dict[str, Any]:
        params = job.params or {}
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", str(params.get("name") or "imported").rsplit(".", 1)[0]).strip("_")[:40]
        name = stem or "imported"
        if RESERVED_SAVE_NAME.search(name):
            # the game's autosaves and the panel's saves are written under such names later
            name = f"{name}_import"
        existing = {str(e["name"])[: -len(SAVE_SUFFIX)] for e in self._files(client, instance)}
        if name in existing:  # never overwrite a save of the server (the agent refuses it as well)
            name = f"{name}_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"[:64]
        from apps.instances.spec import rcon_password

        result = client.game_action(
            instance.id,
            "upload_save",
            {"path": params["path"], "save_name": name, "load": True, "admin_password": rcon_password(instance)},
            timeout=420,
        )
        session = result.get("session")
        self.definition._remember_session(instance, session)
        ctx.log(f"Imported as {SAVES_DIR}/{name}{SAVE_SUFFIX} and loaded (session '{session}')")
        return {"save": name, "session": session}
