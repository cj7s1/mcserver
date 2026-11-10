"""HTTP client for the node agent API."""

from __future__ import annotations

import logging
import ssl
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import Any
from uuid import UUID

import httpx
from django.conf import settings

from apps.core.errors import ApiError, ServiceUnavailable, UpstreamError

from . import tls
from .models import Node

log = logging.getLogger(__name__)

# Indirection so tests can drive the polling loop of wait_operation with a fake clock.
monotonic = time.monotonic
sleep = time.sleep

# While waiting for an agent operation, connection problems (agent restarting, connection
# refused/reset, timeouts, a proxy answering 502/503/504) are retried with backoff for up to
# this many seconds in a row: the operation keeps running on the node meanwhile.
OPERATION_RECONNECT_GRACE = 300.0
OPERATION_RETRY_MAX_DELAY = 15.0
TRANSIENT_AGENT_STATUSES = frozenset({502, 503, 504})
# Longest single sleep of wait_operation between two calls of its guard (see ``operation_guard``).
GUARD_SLICE = 1.0

# Set by the job runner while a handler runs (apps/jobs/runner.py): wait_operation calls it while
# an operation is still going (after every poll, and between the retries after a lost
# connection). It raises to end the wait: the job was cancelled or marked as failed, or the
# worker is stopping. The agent goes on with its operation; only the waiting ends.
operation_guard: ContextVar[Callable[[], None] | None] = ContextVar("mcpanel_operation_guard", default=None)


# What an agent says it can do (GET /v1/info, "features") is remembered per node for this long.
# "Not supported" is remembered for a shorter time, so an updated agent is noticed soon.
FEATURES_TTL = 300
FEATURES_MISSING_TTL = 60

# The agent's stop first asks the server to save (``save-all flush``, up to 60 s) and then runs
# ``docker stop`` with the requested timeout, so its answer can take about timeout + 75 s.
STOP_EXTRA_SECONDS = 120


class AgentError(ApiError):
    """An error reported by the agent, with its machine-readable code."""

    def __init__(self, message: str, code: str, status_code: int) -> None:
        # Map agent HTTP statuses to sensible API statuses (agent 5xx => 502)
        api_status = status_code if 400 <= status_code < 500 else 502
        super().__init__(message, code=code, status_code=api_status)
        self.agent_status = status_code


class AgentUnavailable(ServiceUnavailable):
    default_code = "agent_unavailable"
    default_detail = "The node agent is unreachable. Check that the agent container is running."


class AgentTimeout(AgentUnavailable):
    """The request reached the agent, but its answer did not arrive in time.

    Unlike a refused or timed-out connection, the agent may still be carrying the request out
    (a recursive delete, a big move): callers of long file operations report "still running".
    """


# Long file manager operations (recursive delete, move of a big folder): the agent works inside
# the request, so the panel waits this long before it reports "still running".
FILE_OPERATION_TIMEOUT = 900.0


class AgentClient:
    def __init__(self, node: Node | None = None, timeout: float | None = None) -> None:
        self.node = node or Node.default()
        self.base = self.node.agent_url.rstrip("/")
        self.timeout = timeout or settings.MCPANEL_AGENT_TIMEOUT
        self.headers = {"Authorization": f"Bearer {self.node.token}", "User-Agent": "mcpanel-api"}
        self._ssl: ssl.SSLContext | bool | None = False  # resolved lazily (may read the agent cert)

    # -- plumbing --------------------------------------------------------------------
    @property
    def ssl_context(self) -> ssl.SSLContext | None:
        """TLS trust for this node (pinned or CA-verified); ``None`` for plain HTTP."""
        if self._ssl is False:
            self._ssl = tls.ssl_context(self.node)
        return self._ssl  # type: ignore[return-value]

    @property
    def verify(self) -> ssl.SSLContext | bool:
        return self.ssl_context or True

    def _transport_error(self, exc: httpx.HTTPError) -> ApiError:
        if tls.is_verification_failure(exc):
            pinned = tls.transport(self.node) == "tls_pinned"
            return tls.AgentTlsError(
                f"The node agent at {self.base} did not present "
                + (
                    "the pinned certificate. If its certificate was regenerated, update the fingerprint of node "
                    f"'{self.node.name}'."
                    if pinned
                    else "a certificate trusted by the panel (set its fingerprint to pin it)."
                )
            )
        return AgentUnavailable(f"Cannot reach the node agent at {self.base}: {exc}")

    def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any) -> httpx.Response:
        url = f"{self.base}{path}"
        try:
            resp = httpx.request(method, url, headers=self.headers, timeout=timeout or self.timeout, verify=self.verify, **kwargs)
        except httpx.ConnectTimeout as exc:
            raise AgentUnavailable(f"The node agent did not respond in time ({method} {path})") from exc
        except httpx.TimeoutException as exc:
            # the request was sent (or is being sent): the agent may still be working on it
            raise AgentTimeout(f"The node agent did not respond in time ({method} {path})") from exc
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from exc
        if resp.status_code >= 400:
            code, message = "agent_error", resp.text[:500]
            try:
                body = resp.json()
                if isinstance(body.get("error"), dict):
                    code = body["error"].get("code", code)
                    message = body["error"].get("message", message)
                elif "detail" in body:
                    message = body["detail"] if isinstance(body["detail"], str) else str(body["detail"])
                    code = "agent_validation_error" if resp.status_code == 422 else code
            except ValueError:
                pass
            if resp.status_code == 401:
                raise UpstreamError("The node agent rejected the API token (check MCPANEL_AGENT_TOKEN)", code="agent_auth")
            raise AgentError(message, code, resp.status_code)
        return resp

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._request(method, path, **kwargs).json()

    # -- host ----------------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self._json("GET", "/v1/health", timeout=5)

    def info(self) -> dict[str, Any]:
        return self._json("GET", "/v1/info")

    def features(self, *, refresh: bool = False) -> frozenset[str]:
        """What this node's agent says it can do beyond the first API version (``features`` of
        GET /v1/info), cached per node. An agent from before the list existed has none.

        An agent that cannot be asked right now counts as having none, and that is not
        remembered: the next call asks again.
        """
        from django.core.cache import cache

        key = f"agent-features:{self.node.pk}:{self.base}"
        cached = None if refresh else cache.get(key)
        if cached is not None:
            return frozenset(cached)
        try:
            reported = self.info().get("features")
        except ApiError as exc:
            log.debug("could not read the features of the agent at %s: %s", self.base, exc)
            return frozenset()
        found = sorted(str(f) for f in reported) if isinstance(reported, list) else []
        cache.set(key, found, FEATURES_TTL if found else FEATURES_MISSING_TTL)
        return frozenset(found)

    def supports(self, feature: str) -> bool:
        return feature in self.features()

    def games(self, *, refresh: bool = False) -> dict[str, dict[str, Any]]:
        """The games this node's agent has runtime profiles for (``games`` of GET /v1/info:
        ``{game: {"variants", "images", "control"}}``), cached per node like ``features``.
        Empty for an agent without the ``games`` feature (it runs Minecraft only) and for an agent
        that cannot be asked right now (not remembered)."""
        from django.core.cache import cache

        key = f"agent-games:{self.node.pk}:{self.base}"
        cached = None if refresh else cache.get(key)
        if cached is not None:
            return dict(cached)
        try:
            reported = self.info().get("games")
        except ApiError as exc:
            log.debug("could not read the games of the agent at %s: %s", self.base, exc)
            return {}
        found = {str(k): (v if isinstance(v, dict) else {}) for k, v in reported.items()} if isinstance(reported, dict) else {}
        cache.set(key, found, FEATURES_TTL if found else FEATURES_MISSING_TTL)
        return found

    def published_ports(self) -> list[dict[str, Any]]:
        return self._json("GET", "/v1/ports")["ports"]

    # -- containers ----------------------------------------------------------------------
    def list_containers(self) -> list[dict[str, Any]]:
        return self._json("GET", "/v1/instances")["containers"]

    def list_state(self) -> dict[str, Any]:
        """``containers`` like :meth:`list_containers`, plus ``last_runs``: the last-run record
        (without console lines) of every server that has one, by instance id. A stopped server
        has no container; agents without the ``retire`` feature report no records."""
        data = self._json("GET", "/v1/instances")
        last_runs = data.get("last_runs")
        return {"containers": data["containers"], "last_runs": last_runs if isinstance(last_runs, dict) else {}}

    def prepare(self, instance_id: UUID, files: dict[str, str] | None = None) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/prepare", json={"files": files or {}})

    def apply(self, instance_id: UUID, spec: dict[str, Any], *, start: bool = False, pull: str = "missing") -> dict[str, Any]:
        """Queue the (re)creation of the container. ``pull``: ``missing`` (only when the tag is not on
        the host), ``newer`` (ask the registry whether the tag points to a newer build; agents with
        the ``pull_newer`` feature only), ``always`` or ``never``. The operation's result carries
        ``container``, ``container_format`` and, from agents with ``pull_newer``, ``image_id_before``,
        ``image_id``, ``pulled`` and ``warnings``."""
        return self._json("PUT", f"/v1/instances/{instance_id}/container", json={"spec": spec, "start": start, "pull": pull})[
            "operation"
        ]

    def remove_container(self, instance_id: UUID, timeout: int = 60) -> bool:
        return self._json(
            "DELETE", f"/v1/instances/{instance_id}/container", params={"timeout": timeout}, timeout=timeout + STOP_EXTRA_SECONDS
        )["removed"]

    def retire(self, instance_id: UUID, *, settle_seconds: float = 10.0) -> dict[str, Any]:
        """Remove the container of a stopped server after the agent saved its last-run record.

        Only agents with the ``retire`` feature. Answers ``{"retired", "reason", "recorded",
        "last_run"}``; no container is success (``retired`` false). The agent refuses a
        container that runs (409 ``container_running``) or stopped less than ``settle_seconds``
        ago (409 ``not_settled``), and keeps the container when the record cannot be written
        (``last_run_failed``).
        """
        return self._json("POST", f"/v1/instances/{instance_id}/retire", json={"settle_seconds": settle_seconds}, timeout=120)

    def forget_last_run(self, instance_id: UUID) -> bool:
        """Delete the last-run record of a server on this node (agents with the ``retire`` feature)."""
        return bool(self._json("DELETE", f"/v1/instances/{instance_id}/last-run").get("deleted"))

    def status(self, instance_id: UUID) -> dict[str, Any]:
        """The container's state. Without a container ``exists`` is false and ``last_run`` (when
        present) is the last-run record: exit code, out-of-memory flag, restart count, times."""
        return self._json("GET", f"/v1/instances/{instance_id}/status")

    def start(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/start", timeout=120)

    def stop(self, instance_id: UUID, timeout: int | None = None) -> dict[str, Any]:
        body = {"timeout": timeout} if timeout else {}
        return self._json("POST", f"/v1/instances/{instance_id}/stop", json=body, timeout=(timeout or 120) + STOP_EXTRA_SECONDS)

    def restart(self, instance_id: UUID, timeout: int | None = None) -> dict[str, Any]:
        body = {"timeout": timeout} if timeout else {}
        return self._json(
            "POST", f"/v1/instances/{instance_id}/restart", json=body, timeout=(timeout or 120) + STOP_EXTRA_SECONDS + 120
        )

    def kill(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/kill")

    def stats(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/stats", timeout=15)

    def disk(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/disk", timeout=120)

    def logs(self, instance_id: UUID, tail: int = 500, *, previous: bool = True) -> list[str]:
        """The last ``tail`` console lines. With a container the agent puts the end of the previous
        run (its last-run record) in front of a short log; ``previous=False`` asks for the
        container's own output only. Without a container the answer is the record either way."""
        params: dict[str, Any] = {"tail": tail}
        if not previous:
            params["previous"] = "false"
        return self._json("GET", f"/v1/instances/{instance_id}/logs", params=params)["lines"]

    def ping(self, instance_id: UUID) -> dict[str, Any]:
        """A Minecraft status ping of the running server (agents with the ``ping`` feature): ``online``,
        ``max``, ``sample`` (a few of the players online), ``version``, ``motd``, ``latency_ms``.
        Unlike an RCON command it leaves no line in the server's log. 409 ``not_running`` without a
        running server."""
        return self._json("GET", f"/v1/instances/{instance_id}/ping", timeout=15)

    def command(self, instance_id: UUID, command: str, timeout: float = 30) -> str:
        return self._json(
            "POST", f"/v1/instances/{instance_id}/command", json={"command": command, "timeout": timeout}, timeout=timeout + 10
        )["output"]

    def delete_data(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("DELETE", f"/v1/instances/{instance_id}/data", json={"confirm": str(instance_id)})["operation"]

    def clone(
        self,
        instance_id: UUID,
        target_id: UUID,
        exclude_worlds: bool = False,
        *,
        level_name: str | None = None,
        layout: str | None = None,
    ) -> dict[str, Any]:
        """Copy the server folder into ``target_id``. With ``exclude_worlds`` the world folders are
        left out: ``level_name`` and ``layout`` (apps.instances.world) say which they are; agents
        from before these fields ignore them and leave out the default names."""
        body: dict[str, Any] = {"target_instance_id": str(target_id), "exclude_worlds": exclude_worlds}
        if level_name:
            body["level_name"] = level_name
        if layout:
            body["layout"] = layout
        return self._json("POST", f"/v1/instances/{instance_id}/clone", json=body)["operation"]

    # -- files ---------------------------------------------------------------------------
    def list_files(self, instance_id: UUID, path: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/files/list", params={"path": path})

    def stat_file(self, instance_id: UUID, path: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/files/stat", params={"path": path})

    def read_text(self, instance_id: UUID, path: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/files/text", params={"path": path})

    def write_text(
        self, instance_id: UUID, path: str, content: str, create_only: bool = False, encoding: str | None = None
    ) -> dict[str, Any]:
        """``encoding`` (``utf-8`` / ``iso-8859-1``) is sent only when given; an agent without it
        ignores the field and writes UTF-8."""
        body: dict[str, Any] = {"path": path, "content": content, "create_only": create_only}
        if encoding is not None:
            body["encoding"] = encoding
        return self._json("PUT", f"/v1/instances/{instance_id}/files/text", json=body)

    def download(
        self, instance_id: UUID, path: str | list[str], *, endpoint: str = "download"
    ) -> tuple[Iterator[bytes], int | None, httpx.Response]:
        """Stream a file (``download``) or a zip of one or more folders (``archive``)."""
        client = httpx.Client(headers=self.headers, timeout=httpx.Timeout(self.timeout, read=300), verify=self.verify)
        try:
            req = client.build_request("GET", f"{self.base}/v1/instances/{instance_id}/files/{endpoint}", params={"path": path})
            resp = client.send(req, stream=True)
        except httpx.HTTPError as exc:
            client.close()
            raise self._transport_error(exc) from exc
        if resp.status_code >= 400:
            resp.read()
            client.close()
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            raise AgentError(err.get("message", "Download failed"), err.get("code", "agent_error"), resp.status_code)
        length = resp.headers.get("content-length")

        def _iter() -> Iterator[bytes]:
            try:
                yield from resp.iter_bytes(256 * 1024)
            finally:
                resp.close()
                client.close()

        return _iter(), int(length) if length else None, resp

    def upload(
        self,
        instance_id: UUID,
        path: str,
        chunks: Iterator[bytes] | bytes,
        overwrite: bool = False,
        sha256: str | None = None,
    ) -> dict[str, Any]:
        """Stream ``chunks`` to ``path``; with ``sha256`` the agent commits only matching bytes."""
        params = {"path": path, "overwrite": str(overwrite).lower()}
        if sha256:
            params["sha256"] = sha256
        return self._json("PUT", f"/v1/instances/{instance_id}/files/upload", params=params, content=chunks, timeout=3600)

    def mkdir(self, instance_id: UUID, path: str, parents: bool = False) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/files/mkdir", json={"path": path, "parents": parents})

    def rename(
        self, instance_id: UUID, source: str, destination: str, overwrite: bool = False, *, timeout: float | None = None
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/instances/{instance_id}/files/rename",
            json={"source": source, "destination": destination, "overwrite": overwrite},
            timeout=timeout,
        )

    def delete_files(
        self, instance_id: UUID, paths: list[str], recursive: bool = False, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """``timeout``: the agent deletes inside the request (a recursive delete of a big folder can
        take minutes); :data:`FILE_OPERATION_TIMEOUT` for deletes a user started."""
        return self._json(
            "POST",
            f"/v1/instances/{instance_id}/files/delete",
            json={"paths": paths, "recursive": recursive},
            timeout=timeout,
        )

    def search_files(self, instance_id: UUID, query: str, path: str = "") -> list[dict[str, Any]]:
        return self._json("GET", f"/v1/instances/{instance_id}/files/search", params={"q": query, "path": path}, timeout=120)[
            "results"
        ]

    def hash_files(
        self, instance_id: UUID, path: str, algorithm: str = "sha1", *, suffixes: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        """SHA digests of the files directly inside ``path``. A file the agent does not read (larger
        than its limit) has ``hash: None`` and ``too_large: true``. ``suffixes`` limits the answer to
        names ending in one of them (agents without the filter ignore it, so callers filter too). An
        agent still reading a big folder answers 503 ``hashing_busy``."""
        params: dict[str, Any] = {"path": path, "algorithm": algorithm}
        if suffixes:
            params["suffix"] = list(suffixes)
        return self._json("GET", f"/v1/instances/{instance_id}/files/hashes", params=params, timeout=300)["files"]

    def hash_files_partial(
        self, instance_id: UUID, path: str, algorithm: str = "sha1", *, wait: float, suffixes: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        """Like :meth:`hash_files`, but the agent answers after about ``wait`` seconds.

        Files it has not hashed yet have ``hash: None``; it keeps hashing in the background.
        Returns ``{"files", "complete", "pending"}`` (agents without progressive hashing
        ignore ``wait`` and always return a complete result). Files too large to hash
        (``too_large``) are never pending.
        """
        params: dict[str, Any] = {"path": path, "algorithm": algorithm, "wait": wait}
        if suffixes:
            params["suffix"] = list(suffixes)
        data = self._json("GET", f"/v1/instances/{instance_id}/files/hashes", params=params, timeout=300)
        files = data.get("files") or []
        pending = sum(1 for f in files if f.get("hash") is None and not f.get("too_large"))
        return {"files": files, "complete": pending == 0, "pending": pending}

    def fetch_file(
        self,
        instance_id: UUID,
        *,
        url: str,
        path: str,
        sha1: str | None = None,
        sha512: str | None = None,
        sha256: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/instances/{instance_id}/files/fetch",
            json={"url": url, "path": path, "sha1": sha1, "sha512": sha512, "sha256": sha256, "overwrite": overwrite},
            timeout=900,
        )

    def extract(
        self,
        instance_id: UUID,
        path: str,
        destination: str = "",
        *,
        strip: str = "auto",
        overwrite: bool = False,
        delete_archive: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/instances/{instance_id}/files/extract",
            json={
                "path": path,
                "destination": destination,
                "strip": strip,
                "overwrite": overwrite,
                "delete_archive": delete_archive,
            },
        )["operation"]

    # -- worlds --------------------------------------------------------------------------
    def world_info(self, instance_id: UUID, level_name: str, layout: str, *, sizes: bool = True) -> dict[str, Any]:
        """The world folders and their state; ``sizes=False`` skips measuring them (no walk over every file)."""
        params = {"level_name": level_name, "layout": layout}
        if not sizes:
            params["sizes"] = "false"
        return self._json("GET", f"/v1/instances/{instance_id}/world", params=params, timeout=120)

    def world_replace(
        self, instance_id: UUID, *, archive: str, level_name: str, layout: str, delete_archive: bool = False
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/instances/{instance_id}/world/replace",
            json={"archive": archive, "level_name": level_name, "layout": layout, "delete_archive": delete_archive},
        )["operation"]

    def world_reset(
        self, instance_id: UUID, *, level_name: str, layout: str, expect: list[str], keep: list[str]
    ) -> dict[str, Any]:
        """Delete the world folders ``expect`` (the agent refuses if they are not exactly the world).

        ``confirm`` repeats the instance id in the body, as for ``delete_data``.
        """
        return self._json(
            "POST",
            f"/v1/instances/{instance_id}/world/reset",
            json={
                "confirm": str(instance_id),
                "level_name": level_name,
                "layout": layout,
                "expect": list(expect),
                "keep": list(keep),
            },
        )["operation"]

    # -- backups -------------------------------------------------------------------------
    def list_backups(self, instance_id: UUID, timeout: float = 300) -> list[dict[str, Any]]:
        return self._json("GET", f"/v1/instances/{instance_id}/backups", timeout=timeout)["snapshots"]

    def create_backup(
        self, instance_id: UUID, backup_id: str, *, tags: list[str], consistency: str = "auto", excludes: list[str] | None = None
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/instances/{instance_id}/backups",
            json={"backup_id": backup_id, "tags": tags, "consistency": consistency, "excludes": excludes or []},
        )["operation"]

    def restore_backup(self, instance_id: UUID, snapshot_id: str, target_instance_id: UUID | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"snapshot_id": snapshot_id}
        if target_instance_id:
            body["target_instance_id"] = str(target_instance_id)
        return self._json("POST", f"/v1/instances/{instance_id}/backups/restore", json=body)["operation"]

    def forget_backups(self, instance_id: UUID, snapshot_ids: list[str], prune: bool = True) -> dict[str, Any]:
        return self._json(
            "POST", f"/v1/instances/{instance_id}/backups/forget", json={"snapshot_ids": snapshot_ids, "prune": prune}
        )["operation"]

    def check_backups(self, instance_id: UUID, read_data_subset: str | None = None) -> dict[str, Any]:
        params = {"read_data_subset": read_data_subset} if read_data_subset else {}
        return self._json("POST", f"/v1/instances/{instance_id}/backups/check", params=params)["operation"]

    def backup_files(self, instance_id: UUID, snapshot_id: str, path: str = "") -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/backups/{snapshot_id}/files", params={"path": path}, timeout=600)

    def backup_download(self, instance_id: UUID, snapshot_id: str, path: str) -> tuple[Iterator[bytes], httpx.Response]:
        client = httpx.Client(headers=self.headers, timeout=httpx.Timeout(self.timeout, read=600), verify=self.verify)
        req = client.build_request(
            "GET", f"{self.base}/v1/instances/{instance_id}/backups/{snapshot_id}/download", params={"path": path}
        )
        try:
            resp = client.send(req, stream=True)
        except httpx.HTTPError as exc:
            client.close()
            raise self._transport_error(exc) from exc
        if resp.status_code >= 400:
            resp.read()
            client.close()
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            raise AgentError(err.get("message", "Download failed"), err.get("code", "agent_error"), resp.status_code)

        def _iter() -> Iterator[bytes]:
            try:
                yield from resp.iter_bytes(256 * 1024)
            finally:
                resp.close()
                client.close()

        return _iter(), resp

    def backup_restore_path(self, instance_id: UUID, snapshot_id: str, path: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/backups/{snapshot_id}/restore-path", json={"path": path})[
            "operation"
        ]

    # -- snapshots -------------------------------------------------------------------------
    def snapshots(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/snapshots")

    def create_snapshot(
        self, instance_id: UUID, label: str = "", consistency: str = "auto", kind: str = "manual"
    ) -> dict[str, Any]:
        return self._json(
            "POST", f"/v1/instances/{instance_id}/snapshots", json={"label": label, "consistency": consistency, "kind": kind}
        )["operation"]

    def delete_snapshot(self, instance_id: UUID, name: str) -> dict[str, Any]:
        # without CAP_SYS_ADMIN the agent deletes a btrfs snapshot file by file, which can take minutes
        return self._json("DELETE", f"/v1/instances/{instance_id}/snapshots/{name}", timeout=1800)

    def restore_snapshot(self, instance_id: UUID, name: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/snapshots/{name}/restore")["operation"]

    def convert_snapshots(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/snapshots/convert")["operation"]

    # -- operations ------------------------------------------------------------------------
    def operation(self, op_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/operations/{op_id}")

    def wait_operation(
        self,
        op_id: str,
        *,
        timeout: float = 3600,
        interval: float = 1.0,
        on_progress: Any = None,
        reconnect_grace: float | None = None,
        guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Poll an agent operation until it finishes. Raises AgentError on failure.

        ``guard`` (default: ``operation_guard``, which the job runner sets) is called while the
        operation is still going; whatever it raises ends the wait. An operation that has
        finished always returns its result (or its error) first.

        Transient errors while polling (see ``is_transient``) are retried with exponential
        backoff for up to ``reconnect_grace`` seconds in a row (default
        ``OPERATION_RECONNECT_GRACE``) instead of abandoning a long job after one blip. Polling
        simply resumes if the agent still knows the operation afterwards. Agent operations live
        in the agent's memory, so if the agent comes back *without* it, it restarted and the
        result is unknown: that is reported as ``operation_lost`` with an explicit message.
        """
        grace = OPERATION_RECONNECT_GRACE if reconnect_grace is None else reconnect_grace
        guard = guard if guard is not None else operation_guard.get()
        deadline = monotonic() + timeout
        outage_since: float | None = None
        delay = interval
        while True:
            try:
                op = self.operation(op_id)
            except AgentError as exc:
                if exc.agent_status == 404:
                    raise AgentError(
                        (
                            "The node agent restarted while this operation was running, so its result is unknown "
                            "(the agent keeps operations in memory only). Check the server's state and files before retrying."
                        )
                        if outage_since is not None
                        else "The agent lost track of this operation (it probably restarted). Check the server's state "
                        "before retrying.",
                        "operation_lost",
                        502,
                    ) from exc
                if not self.is_transient(exc):
                    raise
                outage_since, delay = self._wait_for_agent(op_id, exc, outage_since, delay, interval, grace, deadline, guard)
                continue
            except AgentUnavailable as exc:
                outage_since, delay = self._wait_for_agent(op_id, exc, outage_since, delay, interval, grace, deadline, guard)
                continue
            if outage_since is not None:
                log.info(
                    "Agent %s is back after %.0f s; operation %s is still known", self.base, monotonic() - outage_since, op_id
                )
                outage_since, delay = None, interval
            if on_progress:
                on_progress(op)
            if op["status"] == "succeeded":
                return op
            if op["status"] == "failed":
                raise AgentError(op.get("error") or "Agent operation failed", op.get("error_code") or "operation_failed", 500)
            if monotonic() > deadline:
                raise AgentError("Timed out waiting for the agent operation", "operation_timeout", 504)
            _guarded_sleep(interval, guard)

    @staticmethod
    def is_transient(exc: ApiError) -> bool:
        """Connection-level problems that are expected to clear up (agent restart, proxy hiccup)."""
        if isinstance(exc, AgentUnavailable):
            return True
        return isinstance(exc, AgentError) and exc.agent_status in TRANSIENT_AGENT_STATUSES

    def _wait_for_agent(
        self,
        op_id: str,
        exc: ApiError,
        outage_since: float | None,
        delay: float,
        interval: float,
        grace: float,
        deadline: float,
        guard: Callable[[], None] | None = None,
    ) -> tuple[float, float]:
        """Back off after a transient polling error; raise once the outage lasted too long."""
        now = monotonic()
        if outage_since is None:
            outage_since = now
            log.warning("Lost contact with agent %s while waiting for operation %s: %s (retrying)", self.base, op_id, exc)
        if now - outage_since >= grace or now >= deadline:
            raise AgentUnavailable(
                f"Lost contact with the node agent at {self.base} for {now - outage_since:.0f} s while waiting for an "
                f"operation ({exc.detail}). It may still be running on the node; check the server before retrying."
            ) from exc
        delay = min(max(delay * 2, interval, 0.5), OPERATION_RETRY_MAX_DELAY)
        _guarded_sleep(max(0.0, min(delay, deadline - now)), guard)
        return outage_since, delay

    def ws_url(self, path: str) -> str:
        return self.base.replace("http://", "ws://").replace("https://", "wss://") + path


def _guarded_sleep(seconds: float, guard: Callable[[], None] | None) -> None:
    """Sleep ``seconds``, calling ``guard`` before every slice of at most GUARD_SLICE seconds."""
    if guard is None:
        sleep(seconds)
        return
    end = monotonic() + seconds
    while True:
        guard()
        left = end - monotonic()
        if left <= 0:
            return
        sleep(min(left, GUARD_SLICE))
