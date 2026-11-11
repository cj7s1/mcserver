"""Server files that hold a secret, as the file API and the backup browser treat them.

* **Secret files** (``GameDefinition.secret_paths``: Factorio's ``config/rconpw``, Satisfactory's
  ``.mcpanel/api-token`` and its claim ``saved/ServerSettings.7777.sav``): never downloaded (also not
  from a backup, not inside a zip), never written, overwritten, renamed or deleted through the file
  API, and no folder that contains one is renamed or deleted (a renamed folder would make the file
  downloadable under another name). The panel writes the control secret file itself before every
  apply.
* **Secret-bearing files** (``secret_bearing_paths``: TShock's ``config.json`` with the REST token):
  a download is masked like the text editor (``masked_text``); a zip leaves them out for users
  without ``instance.configure``.

Paths are compared after normalising (no leading ``/``, no ``.`` parts, ``\\`` as ``/``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apps import games
from apps.core.errors import ApiError

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


def _within(path: str, folder: str) -> bool:
    """Whether ``path`` is ``folder`` or lies inside it ("" = the server folder)."""
    return not folder or path == folder or path.startswith(folder + "/")


def is_secret(instance: Instance, path: Any) -> bool:
    return norm(path) in secret_paths(instance)


def is_bearing(instance: Instance, path: Any) -> bool:
    return norm(path) in bearing_paths(instance)


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


def zip_excludes(instance: Instance, folders: list[str], user: Any) -> list[str]:
    """The secret (and, without ``instance.configure``, secret-bearing) files inside ``folders``."""
    candidates = list(secret_paths(instance))
    if not (user is not None and user.has_panel_perm("instance.configure", instance)):
        candidates += list(bearing_paths(instance))
    wanted = [norm(f) for f in folders] or [""]
    return sorted({c for c in candidates if any(_within(c, f) for f in wanted)})


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
