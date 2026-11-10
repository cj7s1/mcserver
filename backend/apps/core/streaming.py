"""Responses that pass a download from the node agent through to the browser.

``AgentClient.download`` / ``backup_download`` return a *synchronous* iterator over the agent's
HTTP stream. Given to a plain ``StreamingHttpResponse``, Django's ASGI handler (the panel runs
under uvicorn) consumes such an iterator with ``sync_to_async(list)``: the whole download (a
world zip of several GB) is read into the panel's memory before the first byte goes out, and a
cancelled download is still read to the end.

:class:`AgentStreamingResponse` iterates asynchronously instead: one chunk at a time is pulled
from the agent in a worker thread and sent before the next is read, so memory stays at about one
chunk and the first bytes arrive at once. When the browser goes away (or anything else ends the
response early) the agent's response is closed, which stops the transfer on the agent too.
Synchronous consumers (WSGI, Django's test client) iterate the same object as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

from asgiref.sync import sync_to_async
from django.http import StreamingHttpResponse

log = logging.getLogger(__name__)

_END = object()
# After the agent's response was closed, how long a chunk that is still being read may take to
# give up (the read fails at once when its connection is closed).
_ABANDON_WAIT = 10.0


def _close_quietly(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        close()
    except ValueError:
        pass  # "generator already executing": its read is still in a worker thread (see below)
    except Exception:  # closing must never turn a finished download into an error
        log.debug("closing the agent stream failed", exc_info=True)


class AgentStreamingResponse(StreamingHttpResponse):
    """A streaming response over an agent download (see the module docstring).

    ``chunks`` is the iterator from ``AgentClient.download`` / ``backup_download`` and
    ``upstream`` the ``httpx.Response`` it reads from (None when there is nothing to close
    besides the iterator, e.g. in tests).
    """

    def __init__(self, chunks: Iterator[bytes], upstream: Any = None, **kwargs: Any) -> None:
        super().__init__(chunks, **kwargs)
        self._upstream = upstream
        self._chunks = chunks
        self._resource_closers.append(self._close_upstream)

    def _close_upstream(self) -> None:
        _close_quietly(self._upstream)
        _close_quietly(self._chunks)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        iterator = self._iterator
        pending: asyncio.Future[Any] | None = None
        try:
            while True:
                # shield: if this coroutine is cancelled (client gone), the read in the worker
                # thread cannot be interrupted; keep a handle on it to wait for it below
                pending = asyncio.ensure_future(sync_to_async(next, thread_sensitive=False)(iterator, _END))
                part = await asyncio.shield(pending)
                pending = None
                if part is _END:
                    return
                yield self.make_bytes(part)
        finally:
            # Closing the agent's response ends a read that is still running and tells the agent
            # to stop; then the iterator (and with it the HTTP client) is closed.
            await sync_to_async(_close_quietly, thread_sensitive=False)(self._upstream)
            if pending is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(pending, _ABANDON_WAIT)
            await sync_to_async(_close_quietly, thread_sensitive=False)(self._chunks)


def read_bytes(client: Any, instance_id: Any, path: str, limit: int) -> bytes:
    """Up to ``limit`` bytes of a file through the agent's download endpoint (raw bytes; its text
    endpoint decodes them as UTF-8)."""
    chunks, _length, upstream = client.download(instance_id, path)
    data = bytearray()
    try:
        for chunk in chunks:
            data += chunk
            if len(data) >= limit:
                break
    finally:
        _close_quietly(upstream)
        _close_quietly(chunks)
    return bytes(data[:limit])
