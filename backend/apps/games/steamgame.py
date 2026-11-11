"""The shared part of the games whose image downloads (or carries) the game itself and follows one
branch: Palworld, Valheim, Project Zomboid, Hollow Knight (HKMP), Counter-Strike 2 and Rust
(contract ``audit-2026-11-09/briefs/games-6-contract.md``, probe ``design/probe-6games.md``).

What they share, after Satisfactory's model:

* **No version to pick** (``fixed_version``): the image runs SteamCMD (or DepotDownloader) at every start
  and follows Steam's public branch, so players' clients and the server stay on the same build. The
  runtime image itself is pinned by the panel release (an exact tag, Rust a digest); the
  ``runtime_images`` setting may name another repository. "Update" means a restart.
* **Memory model "plain"**: the container limit is the memory the owner chose.
* **Restart policy "no"**: only the agent starts the container (a crash stays stopped and shows in the
  server's status and diagnostics); the agent's profile forces the same.
* **Virtual configs**: settings the image takes from its environment are kept by the panel
  (``Instance.game_options[<config id>]``) and passed as variables at every start
  (``config_values`` / ``write_virtual_config``); a change needs a restart (the spec changes).
* **Named ports published 1:1** where the image takes the port from a variable
  (``PortSpec.one_to_one``): the container listens on the host port's number, because Steam listings
  and some clients use the port the server advertises.
* **Diagnostics** for the failures every such image has: the download (SteamCMD / DepotDownloader),
  a full disk and running out of memory.

Every value that reaches the environment is checked with the same pattern the agent's profile checks
(config fields carry a ``pattern``; secrets a ``SecretSpec.pattern``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar

from apps.core.errors import ApiError

from .base import ConfigFile, GameDefinition, PortSpec

if TYPE_CHECKING:
    from apps.instances.models import Instance
    from apps.nodes.client import AgentClient


# Text a server's name may hold when it reaches an image's environment: printable, no quotes, no
# backslash, no shell metacharacters (several of these images write it into a file or a start line).
SAFE_TEXT = r"[A-Za-z0-9 _.,:;!?()\[\]@#%+=/'-]"
SAFE_NAME_PATTERN = rf"^(?! ){SAFE_TEXT}{{1,64}}(?<! )$"
SAFE_NAME_HELP = "1 to 64 letters, digits, spaces and . , : ; ! ? ( ) [ ] @ # % + = / ' - _ (no quotes or backslashes)"


def safe_name(text: str, default: str, limit: int = 64) -> str:
    """``text`` reduced to the characters of SAFE_TEXT (a default server name from the panel's name)."""
    cleaned = " ".join(re.sub(rf"(?!{SAFE_TEXT}).", " ", text or "").split())[:limit].strip()
    return cleaned or default


class SteamGameDefinition(GameDefinition):
    """Base of the six games of the contract (see the module docstring)."""

    memory_model = "plain"
    repository: str = ""
    image_tag: str = ""  # the pinned tag (``repository:tag``)
    image_digest: str = ""  # or a pinned digest (``repository@sha256:...``; Rust)
    default_memory: int = 4096
    branch: str = "public"
    virtual_configs: tuple[ConfigFile, ...] = ()
    file_configs: tuple[ConfigFile, ...] = ()
    clean_exit_codes = frozenset({0, 143})
    allowed_user_env = frozenset({"TZ"})
    capability_set = frozenset({"diagnostics"})
    # patterns of options that name files or reach the environment: {key: (pattern, help)}
    option_patterns: ClassVar[dict[str, tuple[str, str]]] = {}

    # -- data ------------------------------------------------------------------------------------
    def default_memory_mb(self) -> int:
        return self.default_memory

    def config_files(self) -> tuple[ConfigFile, ...]:
        return self.virtual_configs + self.file_configs

    def variant(self, instance: Instance) -> str:
        return ""

    def image(self, instance: Instance) -> str:
        repo = self.runtime_repository(self.repository)
        if self.image_digest:
            return f"{repo}@{self.image_digest}"
        return f"{repo}:{self.image_tag}"

    # -- options ---------------------------------------------------------------------------------
    def validate_options(self, values: dict[str, Any] | None) -> dict[str, Any]:
        """The base checks (type, enum), then each option's ``pattern`` (it names files or reaches the
        environment): ``option_patterns`` {key: (pattern, help)}."""
        out = super().validate_options(values)
        for key, (pattern, help_text) in self.option_patterns.items():
            if key in out and not re.fullmatch(pattern, str(out[key])):
                label = next((o.label for o in self.options if o.key == key), key)
                raise ApiError(f"{label}: {help_text}", code="invalid_option")
        return out

    # -- virtual configs (kept by the panel, passed as environment variables) ----------------------
    def is_virtual_config(self, cfg: ConfigFile) -> bool:
        return any(c.file_id == cfg.file_id for c in self.virtual_configs)

    def virtual_defaults(self, file_id: str, instance: Instance | None = None) -> dict[str, Any]:
        cfg = next(c for c in self.virtual_configs if c.file_id == file_id)
        return {f["key"]: f.get("default") for f in cfg.fields}

    def config_values(self, instance: Instance, file_id: str) -> dict[str, Any]:
        """A virtual config's values: its defaults, then what the panel stored."""
        values = self.virtual_defaults(file_id, instance)
        stored = (instance.game_options or {}).get(file_id)
        if isinstance(stored, dict):
            values.update({k: v for k, v in stored.items() if k in values})
        return values

    def virtual_config_values(self, client: AgentClient, instance: Instance, cfg: ConfigFile) -> dict[str, Any] | None:
        return self.config_values(instance, cfg.file_id)

    def check_config(self, file_id: str, values: dict[str, Any], *, instance: Instance | None = None, data: Any = None) -> None:
        """Rules across the fields of a virtual config (ApiError ``invalid_setting``); default none."""
        return None

    def write_virtual_config(
        self, client: AgentClient, instance: Instance, cfg: ConfigFile, values: dict[str, Any]
    ) -> dict[str, Any]:
        from apps.instances.models import Instance as InstanceModel
        from apps.instances.services import bump_revision

        merged = self.config_values(instance, cfg.file_id)
        merged.update(values)
        self.check_config(cfg.file_id, merged, instance=instance)
        options = dict(instance.game_options or {})
        options[cfg.file_id] = merged
        instance.game_options = options
        InstanceModel.objects.filter(pk=instance.pk).update(game_options=options)
        bump_revision(instance)
        return merged

    def initial_config(self, file_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """Values of a virtual config a new server starts with besides the defaults (e.g. the server
        name from the panel's name); the create request's settings win."""
        return {}

    # -- creating a server -----------------------------------------------------------------------
    def create_fields(self, data: dict[str, Any]) -> dict[str, Any]:
        options = self.validate_options(data.get("options"))
        settings = data.get("settings") or {}
        for cfg in self.virtual_configs:
            values = self.virtual_defaults(cfg.file_id)
            values.update(self.initial_config(cfg.file_id, data))
            values.update(settings.get(cfg.file_id) or {})
            try:
                self.check_config(cfg.file_id, values, data=data)
            except ApiError as exc:
                from rest_framework.exceptions import ValidationError

                raise ValidationError({"settings": [str(exc.detail)]}) from exc
            options[cfg.file_id] = values
        return {
            "server_type": "",
            "minecraft_version": self.branch_of(options),
            "loader_version": "",
            "game_options": options,
            "memory_mb": int(data.get("memory_mb") or self.default_memory_mb()),
            "memory_overhead_mb": 0,
            "stop_timeout": self.default_stop_timeout,
            "restart_policy": "no",
            "jvm_flags_preset": "none",
            "jvm_args": "",
        }

    def branch_of(self, options: dict[str, Any]) -> str:
        """The branch the server follows (shown as its version)."""
        return self.branch

    # -- container -------------------------------------------------------------------------------
    def build_spec(self, instance: Instance) -> dict[str, Any]:
        spec = super().build_spec(instance)
        # only the agent starts it: Docker never does, not after an exit (the console's exit, a crash)
        # nor after a daemon restart (the agent's profile forces the same)
        spec["restart_policy"] = "no"
        return spec

    def port_enabled(self, instance: Instance, port: PortSpec) -> bool:
        if port.when is None:
            return True
        file_id, key, value = port.when
        return self.config_values(instance, file_id).get(key) == value

    # -- the world (games with ``foldersaves.FolderSaves``) -----------------------------------------------
    def world_reset_fields(self) -> tuple[dict[str, Any], ...]:
        """The fields of a world reset (``saves/reset`` ``settings``)."""
        return ()

    def world_label(self, instance: Instance) -> str:
        return ""

    def world_paths(self, client: AgentClient, instance: Instance, purpose: str, params: dict[str, Any]) -> list[str]:
        """The paths of the current world (``purpose``: reset, import or export)."""
        return []

    def world_target(self, client: AgentClient, instance: Instance, root_name: str, params: dict[str, Any]) -> str:
        raise NotImplementedError

    def place_world(self, client: AgentClient, instance: Instance, source: str, target: str) -> str | None:
        """Move an unpacked world folder into place (the import job, after the old one was set aside);
        returns where the world went when that is not ``target``."""
        client.rename(instance.id, source, target, timeout=600)
        return None

    def after_import(self, client: AgentClient, instance: Instance, target: str, params: dict[str, Any]) -> None:
        return None

    def prepare_world_reset(self, client: AgentClient, instance: Instance, values: dict[str, Any], seed: Any) -> str:
        """What the next start creates the new world from; returns its seed for the log ("" = none)."""
        return ""

    def save_now(self, client: AgentClient, instance: Instance) -> bool:
        """Make the running server write its world now; whether it did."""
        return False

    def world_notes(self, instance: Instance) -> list[str]:
        return []

    # -- console helpers ---------------------------------------------------------------------------
    @staticmethod
    def chat_text(text: str, message: str = "", limit: int = 200) -> str:
        """An in-game message as one plain line: no control characters, no quotes, no backslash, no
        ``^`` and no ``;`` (consoles split or interpret them)."""
        line = text if not message else f"{text} - {message}"
        line = re.sub(r"[\x00-\x1f\x7f\"\\^;`$]", " ", line)
        return " ".join(line.split()).lstrip("/-").strip()[:limit]

    # -- diagnostics -----------------------------------------------------------------------------
    def analyze_logs(self, instance: Instance, lines: list[str]) -> list[Any]:
        return steam_issues(self, instance, lines)

    # -- software (no update: the server follows its branch) -----------------------------------------
    software_keys = ("game",)  # nothing a restore could set back

    def software_state(self, instance: Instance) -> dict[str, Any]:
        return {"game": self.id, "branch": instance.minecraft_version or self.branch, "image": instance.image}

    def describe_software(self, state: dict[str, Any]) -> str:
        return f"{self.label} ({state.get('branch') or self.branch} branch)"

    def set_software(self, instance: Instance, state: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        return {}


# --- diagnostics -------------------------------------------------------------------------------------
_DOWNLOAD_FAILED = re.compile(
    r"Error! App '\d+' state is 0x[0-9a-f]+ after update job|ERROR! (?:Failed to install app|Download item \d+ failed)|"
    r"Missing configuration|steamcmd.*(?:Timed out|Connection timed out|No connection)|"
    r"Unable to download|Error downloading|Connection to Steam failed|Failed to (?:download|connect to) (?:depot|Steam)|"
    r"Failed to install (?:app|server)|update (?:failed|aborted)",
    re.IGNORECASE,
)
_DISK_FULL = re.compile(r"No space left on device|Disk write failure|Not enough (?:disk )?space", re.IGNORECASE)
_OUT_OF_MEMORY = re.compile(
    r"OutOfMemoryError|Out of memory|std::bad_alloc|Cannot allocate memory|Killed\s*$|"
    r"Fatal error.*(?:memory|allocat)|exit code 137",
    re.IGNORECASE,
)


def _grep(lines: list[str], rx: re.Pattern[str], limit: int = 5) -> list[str]:
    return [ln[:400] for ln in lines if rx.search(ln)][:limit]


def steam_issues(definition: GameDefinition, instance: Instance, lines: list[str]) -> list[Any]:
    """Known problems of an image that downloads its game: the download failed, the disk is full, the
    server ran out of memory (the agent's out-of-memory flag or the log)."""
    from apps.instances.diagnostics import Issue

    issues: list[Any] = []
    if evidence := _grep(lines, _DISK_FULL):
        issues.append(
            Issue(
                "disk_full",
                "error",
                "The disk is full",
                f"The server could not write its files (the game download needs several GB: {definition.label}'s notes "
                "say how much). Free space on the node's disk, then start the server again.",
                [{"kind": "console", "label": "Open console"}],
                evidence,
            )
        )
    if evidence := _grep(lines, _DOWNLOAD_FAILED):
        issues.append(
            Issue(
                "game_download_failed",
                "error",
                "The game download failed",
                "The image downloads the game from Steam at every start, and that failed (Steam unreachable, a timeout "
                "or a full disk). Start the server again; when it keeps failing, check the node's internet access and "
                "free disk space.",
                [{"kind": "console", "label": "Open console"}],
                evidence,
            )
        )
    oom_lines = _grep(lines, _OUT_OF_MEMORY)
    if getattr(instance, "oom_killed", False) or oom_lines:
        issues.append(
            Issue(
                "out_of_memory",
                "error",
                "The server ran out of memory",
                f"The server needs more memory than its limit ({instance.memory_mb} MB). {definition.label} needs at "
                f"least {definition.min_memory_mb} MB; raise the limit in Settings.",
                [{"kind": "settings", "label": "Increase memory", "field": "memory_mb"}],
                oom_lines,
            )
        )
    return issues


__all__ = ["SAFE_NAME_HELP", "SAFE_NAME_PATTERN", "SteamGameDefinition", "safe_name", "steam_issues"]
