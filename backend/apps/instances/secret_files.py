"""Server files that hold a secret, as the file API and the backup browser treat them.

* **Secret files** (``GameDefinition.secret_paths``: Factorio's ``config/rconpw``, Satisfactory's
  ``.mcpanel/api-token`` and its claim ``saved/ServerSettings.7777.sav``): never downloaded (also not
  from a backup, not inside a zip), never written, overwritten, renamed or deleted through the file
  API, and no folder that contains one is renamed or deleted (a renamed folder would make the file
  downloadable under another name). The panel writes the control secret file itself before every
  apply.
* **Secret-bearing files** (``secret_bearing_paths``: TShock's ``config.json`` with the REST token; and every
  file of ``secret_bearing_folders``: Project Zomboid's logs): a download is masked like the text editor
  (``masked_text``; streamed line by line for the six Steam-era games, ``masks_by_line``); a zip leaves them out
  for users without ``instance.configure`` (for the six games: for everyone, ``bearing_in_zips_for_configure``),
  and a zip of nothing but them is refused (``require_zippable``).
* A **secret folder** (in ``secret_paths``: Project Zomboid's own backup zips, whose ini holds the RCON password
  and cannot be masked) covers everything inside it: never downloaded, zipped, written or moved out.

Paths are compared after normalising (no leading ``/``, no ``.`` parts, ``\\`` as ``/``).

* **Archive extracts and imports** (job ``extract_archive``: the file manager's Extract and the
  Import of a server archive) never write a secret file: the agent skips every entry that lands on
  one of ``extract_protected`` (after its own path normalisation and the strip of a common folder;
  compared ignoring case), and with the panel's folder ``.mcpanel`` holding one (Satisfactory's API
  token), everything inside that folder (also the agent's claim marker). An agent without the
  feature ``extract_protected`` would write them: such an extract is refused for those games.
  Secret-bearing files (TShock's ``config.json``) are written as the file API writes them: the
  panel puts its keys back before every apply (``before_apply``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apps import games
from apps.core.errors import ApiError, ConflictError

if TYPE_CHECKING:
    from apps.instances.models import Instance

MANAGED_FILE_MESSAGE = (
    "This file holds the server's control secret; the panel manages it and does not hand it out or let it be "
    "changed through the file manager"
)
MASKED_DOWNLOAD_LIMIT = 4 * 1024 * 1024  # a secret-bearing file is masked in memory


def norm(path: Any) -> str:
    return "/".join(p for p in str(path or "").replace("\\", "/").split("/") if p and p != ".")


def secret_paths(instance: Instance) -> tuple[str, ...]:
    definition = games.get_or_none(instance.game)
    return tuple(norm(p) for p in definition.secret_paths(instance)) if definition is not None else ()


def bearing_paths(instance: Instance) -> tuple[str, ...]:
    definition = games.get_or_none(instance.game)
    return tuple(norm(p) for p in definition.secret_bearing_paths(instance)) if definition is not None else ()


def bearing_folders(instance: Instance) -> tuple[str, ...]:
    definition = games.get_or_none(instance.game)
    folders = definition.secret_bearing_folders(instance) if definition is not None else ()
    return tuple(f for f in (norm(p) for p in folders) if f)


def _within(path: str, folder: str) -> bool:
    """Whether ``path`` is ``folder`` or lies inside it ("" = the server folder)."""
    return not folder or path == folder or path.startswith(folder + "/")


def is_secret(instance: Instance, path: Any) -> bool:
    """``path`` is a secret file, or lies inside a secret folder (Project Zomboid's own backups)."""
    p = norm(path)
    return bool(p) and any(_within(p, s) for s in secret_paths(instance))


def is_bearing(instance: Instance, path: Any) -> bool:
    """``path`` is a secret-bearing file, or lies inside a secret-bearing folder (Project Zomboid's logs)."""
    p = norm(path)
    return p in bearing_paths(instance) or (bool(p) and any(_within(p, f) for f in bearing_folders(instance)))


def contains_secret(instance: Instance, path: Any) -> bool:
    """``path`` is a secret file or a folder with one inside."""
    p = norm(path)
    return any(_within(s, p) for s in secret_paths(instance))


def refuse(message: str = MANAGED_FILE_MESSAGE) -> ApiError:
    return ApiError(message, code="managed_file", status_code=403)


def require_not_secret(instance: Instance, path: Any) -> None:
    if is_secret(instance, path):
        raise refuse()


def require_no_secret_inside(instance: Instance, path: Any) -> None:
    if contains_secret(instance, path):
        raise refuse(
            "This folder holds the server's control secret, which the panel manages; it cannot be renamed or "
            "deleted through the file manager"
            if not is_secret(instance, path)
            else MANAGED_FILE_MESSAGE
        )


def require_not_moved_out(instance: Instance, source: Any) -> None:
    """A file inside a secret folder (Project Zomboid's own backups) is never renamed: at its new place it
    would be downloadable. (Deleting it is fine.)"""
    if is_secret(instance, source) and not contains_secret(instance, source):
        raise refuse("This file is in a folder the panel does not hand out; it cannot be moved out of it")


PANEL_FOLDER = ".mcpanel"  # the panel's (and the agent's) own folder in a server folder, when a game has one
EXTRACT_PROTECTED_FEATURE = "extract_protected"  # the agent skips the archive entries the panel names


def extract_protected(instance: Instance) -> list[str]:
    """What an archive extract or import must never write (``[]``: Minecraft, Terraria): the secret
    files, and the whole panel folder when one of them lives there."""
    definition = games.get_or_none(instance.game)
    # secret files the game writes itself and the panel deletes anyway (TShock's setup-code.txt): an archive may
    # write them, so the request stays as it was for such games (older agents keep working)
    writable = {norm(p) for p in definition.extract_writable_secret_paths(instance)} if definition is not None else set()
    paths = set(secret_paths(instance)) - writable
    if any(_within(p, PANEL_FOLDER) for p in paths):
        paths = {p for p in paths if not _within(p, PANEL_FOLDER)} | {PANEL_FOLDER}
    return sorted(paths)


def require_extract_destination(instance: Instance, destination: Any) -> None:
    """An archive is never extracted into a protected folder or onto a secret file (every entry
    would be skipped there); compared ignoring case, as the agent compares."""
    dest = norm(destination).casefold()
    if dest and any(_within(dest, p.casefold()) for p in extract_protected(instance)):
        raise refuse("This folder holds files the panel manages for the server; archives cannot be extracted into it")


def require_extract_support(instance: Instance, client: Any) -> list[str]:
    """``extract_protected(instance)``, refused (409 ``agent_outdated``) when there is something to
    protect and the node's agent cannot skip it (it would extract the secret files as they are)."""
    protected = extract_protected(instance)
    if protected and not client.supports(EXTRACT_PROTECTED_FEATURE):
        raise ConflictError(
            "The node's agent is too old to keep the server's secret files when it extracts an archive. Update the agent first.",
            code="agent_outdated",
        )
    return protected


def _bearing_zipped_for(instance: Instance, user: Any) -> bool:
    """Whether secret-bearing files go into this user's zips: with ``instance.configure``, unless the game's
    files hold its control secret (``bearing_in_zips_for_configure`` False: the six Steam-era games)."""
    definition = games.get_or_none(instance.game)
    if definition is not None and not definition.bearing_in_zips_for_configure:
        return False
    return user is not None and user.has_panel_perm("instance.configure", instance)


def zip_excludes(instance: Instance, folders: list[str], user: Any) -> list[str]:
    """The secret (and, without ``instance.configure``, secret-bearing) files and folders inside ``folders``."""
    candidates = list(secret_paths(instance))
    if not _bearing_zipped_for(instance, user):
        candidates += list(bearing_paths(instance)) + list(bearing_folders(instance))
    wanted = [norm(f) for f in folders] or [""]
    return sorted({c for c in candidates if any(_within(c, f) for f in wanted)})


def require_zippable(instance: Instance, folders: list[str], user: Any) -> None:
    """A zip of a folder inside a secret folder, or of a secret-bearing one the user's zips leave out, would
    hold nothing but those files: refused (403 ``managed_file``); single files are downloaded masked."""
    for folder in folders:
        if is_secret(instance, folder):
            raise refuse("This folder holds files with the server's control secret that cannot be masked; it is not handed out")
        if not _bearing_zipped_for(instance, user) and is_bearing(instance, folder) and norm(folder):
            raise refuse("These files may hold the server's passwords: download them one by one (masked), not as a zip")


def masked_bytes(instance: Instance, path: str, chunks: Any) -> bytes:
    """A secret-bearing file read whole and masked like the text editor shows it."""
    data = bytearray()
    for chunk in chunks:
        data += chunk
        if len(data) > MASKED_DOWNLOAD_LIMIT:
            raise ApiError("This file is too large to be downloaded masked", code="too_large", status_code=413)
    definition = games.get_or_none(instance.game)
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise refuse("This file holds a secret and is not text the panel can mask; it cannot be downloaded") from exc
    if definition is not None:
        text = definition.masked_text(instance, path, text)
    return text.encode("utf-8")


def masked_stream(instance: Instance, path: str, chunks: Any) -> Any:
    """A secret-bearing file of a game that masks line by line (``masks_by_line``), masked while it is
    streamed: any size, any encoding (bytes that are not UTF-8 pass unchanged)."""
    definition = games.get_or_none(instance.game)
    pending = b""

    def mask(line: bytes) -> bytes:
        text = line.decode("utf-8", errors="surrogateescape")
        if definition is not None:
            text = definition.masked_text(instance, path, text)
        return text.encode("utf-8", errors="surrogateescape")

    for chunk in chunks:
        pending += chunk
        if b"\n" not in pending:
            if len(pending) > MASKED_DOWNLOAD_LIMIT:  # one line this long: masked as it is, not held forever
                yield mask(pending)
                pending = b""
            continue
        complete, _, pending = pending.rpartition(b"\n")
        yield b"".join(mask(line) + b"\n" for line in complete.split(b"\n"))
    if pending:
        yield mask(pending)


def masked_by_line(instance: Instance) -> bool:
    definition = games.get_or_none(instance.game)
    return bool(definition is not None and definition.masks_by_line)


def mask_search_results(instance: Instance, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Search results without secret files, with lines masked like the editor."""
    definition = games.get_or_none(instance.game)
    out = []
    for r in results:
        path = str(r.get("path") or "")
        if is_secret(instance, path):
            continue
        if definition is not None and isinstance(r.get("text"), str):
            r = {**r, "text": definition.masked_text(instance, path, r["text"])}
        out.append(r)
    return out
