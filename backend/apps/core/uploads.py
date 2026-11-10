"""Upload plumbing: keep a big upload on disk once, not twice.

Under ASGI Django first writes the whole request body to a spool file (``read_body``; the panel
puts it in FILE_UPLOAD_TEMP_DIR, see mcpanel/asgi.py), and the multipart parser then copies every
uploaded file out of it into a ``TemporaryUploadedFile``. Both copies used to stay on disk while
the views forwarded the files to the node agent, which can take minutes for a 2 GiB world.
:class:`ReleaseRequestBodyHandler`, the last upload handler, empties the spool file as soon as
the parser has finished: from then on the uploaded files exist once.
"""

from __future__ import annotations

import logging
import os
import tempfile

from django.core.files.uploadhandler import FileUploadHandler

log = logging.getLogger(__name__)


def upload_dir_problem() -> str | None:
    """Why FILE_UPLOAD_TEMP_DIR cannot take a spooled request body or upload, or None when it can
    (a file is created and removed in it: permission bits alone do not prove it)."""
    from django.conf import settings

    path = settings.FILE_UPLOAD_TEMP_DIR
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".probe-"):
            pass
    except OSError as exc:
        return f"the upload folder {path} (MCPANEL_UPLOAD_TMP) is not writable by this process (uid {os.getuid()}): {exc}"
    return None


def ensure_upload_dir() -> str | None:
    """Create FILE_UPLOAD_TEMP_DIR (0700, owned by the user of this process) when it is missing,
    and check it. Returns the problem (also logged as an error), or None. Called when the web
    process starts (mcpanel/asgi.py), never when the settings are imported."""
    from django.conf import settings

    try:
        os.makedirs(settings.FILE_UPLOAD_TEMP_DIR, mode=0o700, exist_ok=True)
    except OSError as exc:
        log.error("the upload folder %s cannot be created: %s", settings.FILE_UPLOAD_TEMP_DIR, exc)
    problem = upload_dir_problem()
    if problem:
        log.error("%s; requests above %d bytes fail until it is fixed", problem, settings.FILE_UPLOAD_MAX_MEMORY_SIZE)
    return problem


class ReleaseRequestBodyHandler(FileUploadHandler):
    """Passes every chunk on unchanged; after the parser finished, truncates the body spool file."""

    def receive_data_chunk(self, raw_data: bytes, start: int) -> bytes:
        return raw_data

    def file_complete(self, file_size: int) -> None:
        return None  # the file object comes from the handlers before this one

    def upload_complete(self) -> None:
        body = getattr(self.request, "_stream", None)
        if isinstance(body, tempfile.SpooledTemporaryFile):
            try:
                body.truncate(0)
            except (OSError, ValueError):
                log.debug("could not release the request body spool", exc_info=True)
