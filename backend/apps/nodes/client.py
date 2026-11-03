"""HTTP client for the node agent API."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import httpx
from django.conf import settings

from apps.core.errors import ApiError, ServiceUnavailable, UpstreamError

from .models import Node

log = logging.getLogger(__name__)


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


class AgentClient:
    def __init__(self, node: Node | None = None, timeout: float | None = None) -> None:
        self.node = node or Node.default()
        self.base = self.node.agent_url.rstrip("/")
        self.timeout = timeout or settings.MCPANEL_AGENT_TIMEOUT
        self.headers = {"Authorization": f"Bearer {self.node.token}", "User-Agent": "mcpanel-api"}

    # -- plumbing --------------------------------------------------------------------
    def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any) -> httpx.Response:
        url = f"{self.base}{path}"
        try:
            resp = httpx.request(method, url, headers=self.headers, timeout=timeout or self.timeout, **kwargs)
        except httpx.TimeoutException as exc:
            raise AgentUnavailable(f"The node agent did not respond in time ({method} {path})") from exc
        except httpx.HTTPError as exc:
            raise AgentUnavailable(f"Cannot reach the node agent at {self.base}: {exc}") from exc
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

    def published_ports(self) -> list[dict[str, Any]]:
        return self._json("GET", "/v1/ports")["ports"]

    # -- containers ----------------------------------------------------------------------
    def list_containers(self) -> list[dict[str, Any]]:
        return self._json("GET", "/v1/instances")["containers"]

    def prepare(self, instance_id: UUID, files: dict[str, str] | None = None) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/prepare", json={"files": files or {}})

    def apply(self, instance_id: UUID, spec: dict[str, Any], *, start: bool = False, pull: str = "missing") -> dict[str, Any]:
        return self._json("PUT", f"/v1/instances/{instance_id}/container", json={"spec": spec, "start": start, "pull": pull})[
            "operation"
        ]

    def remove_container(self, instance_id: UUID, timeout: int = 60) -> bool:
        return self._json("DELETE", f"/v1/instances/{instance_id}/container", params={"timeout": timeout}, timeout=timeout + 30)[
            "removed"
        ]

    def status(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/status")

    def start(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/start", timeout=120)

    def stop(self, instance_id: UUID, timeout: int | None = None) -> dict[str, Any]:
        body = {"timeout": timeout} if timeout else {}
        return self._json("POST", f"/v1/instances/{instance_id}/stop", json=body, timeout=(timeout or 120) + 60)

    def restart(self, instance_id: UUID, timeout: int | None = None) -> dict[str, Any]:
        body = {"timeout": timeout} if timeout else {}
        return self._json("POST", f"/v1/instances/{instance_id}/restart", json=body, timeout=(timeout or 120) + 120)

    def kill(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/kill")

    def stats(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/stats", timeout=15)

    def disk(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/disk", timeout=120)

    def logs(self, instance_id: UUID, tail: int = 500) -> list[str]:
        return self._json("GET", f"/v1/instances/{instance_id}/logs", params={"tail": tail})["lines"]

    def command(self, instance_id: UUID, command: str, timeout: float = 30) -> str:
        return self._json(
            "POST", f"/v1/instances/{instance_id}/command", json={"command": command, "timeout": timeout}, timeout=timeout + 10
        )["output"]

    def delete_data(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("DELETE", f"/v1/instances/{instance_id}/data", json={"confirm": str(instance_id)})["operation"]

    def clone(self, instance_id: UUID, target_id: UUID, exclude_worlds: bool = False) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/instances/{instance_id}/clone",
            json={"target_instance_id": str(target_id), "exclude_worlds": exclude_worlds},
        )["operation"]

    # -- files ---------------------------------------------------------------------------
    def list_files(self, instance_id: UUID, path: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/files/list", params={"path": path})

    def stat_file(self, instance_id: UUID, path: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/files/stat", params={"path": path})

    def read_text(self, instance_id: UUID, path: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/instances/{instance_id}/files/text", params={"path": path})

    def write_text(self, instance_id: UUID, path: str, content: str, create_only: bool = False) -> dict[str, Any]:
        return self._json(
            "PUT", f"/v1/instances/{instance_id}/files/text", json={"path": path, "content": content, "create_only": create_only}
        )

    def download(self, instance_id: UUID, path: str) -> tuple[Iterator[bytes], int | None, httpx.Response]:
        client = httpx.Client(headers=self.headers, timeout=httpx.Timeout(self.timeout, read=300))
        try:
            req = client.build_request("GET", f"{self.base}/v1/instances/{instance_id}/files/download", params={"path": path})
            resp = client.send(req, stream=True)
        except httpx.HTTPError as exc:
            client.close()
            raise AgentUnavailable(f"Cannot reach the node agent: {exc}") from exc
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

    def upload(self, instance_id: UUID, path: str, chunks: Iterator[bytes] | bytes, overwrite: bool = False) -> dict[str, Any]:
        return self._json(
            "PUT",
            f"/v1/instances/{instance_id}/files/upload",
            params={"path": path, "overwrite": str(overwrite).lower()},
            content=chunks,
            timeout=3600,
        )

    def mkdir(self, instance_id: UUID, path: str, parents: bool = False) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/files/mkdir", json={"path": path, "parents": parents})

    def rename(self, instance_id: UUID, source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/instances/{instance_id}/files/rename",
            json={"source": source, "destination": destination, "overwrite": overwrite},
        )

    def delete_files(self, instance_id: UUID, paths: list[str], recursive: bool = False) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/files/delete", json={"paths": paths, "recursive": recursive})

    def search_files(self, instance_id: UUID, query: str, path: str = "") -> list[dict[str, Any]]:
        return self._json("GET", f"/v1/instances/{instance_id}/files/search", params={"q": query, "path": path}, timeout=120)[
            "results"
        ]

    def hash_files(self, instance_id: UUID, path: str, algorithm: str = "sha1") -> list[dict[str, Any]]:
        return self._json(
            "GET", f"/v1/instances/{instance_id}/files/hashes", params={"path": path, "algorithm": algorithm}, timeout=300
        )["files"]

    def fetch_file(
        self,
        instance_id: UUID,
        *,
        url: str,
        path: str,
        sha1: str | None = None,
        sha512: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        return self._json(
            "POST",
            f"/v1/instances/{instance_id}/files/fetch",
            json={"url": url, "path": path, "sha1": sha1, "sha512": sha512, "overwrite": overwrite},
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

    # -- backups -------------------------------------------------------------------------
    def list_backups(self, instance_id: UUID) -> list[dict[str, Any]]:
        return self._json("GET", f"/v1/instances/{instance_id}/backups", timeout=300)["snapshots"]

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
        client = httpx.Client(headers=self.headers, timeout=httpx.Timeout(self.timeout, read=600))
        req = client.build_request(
            "GET", f"{self.base}/v1/instances/{instance_id}/backups/{snapshot_id}/download", params={"path": path}
        )
        try:
            resp = client.send(req, stream=True)
        except httpx.HTTPError as exc:
            client.close()
            raise AgentUnavailable(f"Cannot reach the node agent: {exc}") from exc
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
        return self._json("DELETE", f"/v1/instances/{instance_id}/snapshots/{name}")

    def restore_snapshot(self, instance_id: UUID, name: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/snapshots/{name}/restore")["operation"]

    def convert_snapshots(self, instance_id: UUID) -> dict[str, Any]:
        return self._json("POST", f"/v1/instances/{instance_id}/snapshots/convert")["operation"]

    # -- operations ------------------------------------------------------------------------
    def operation(self, op_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/operations/{op_id}")

    def wait_operation(
        self, op_id: str, *, timeout: float = 3600, interval: float = 1.0, on_progress: Any = None
    ) -> dict[str, Any]:
        """Poll an agent operation until it finishes. Raises AgentError on failure."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                op = self.operation(op_id)
            except AgentError as exc:
                if exc.agent_status == 404:
                    raise AgentError(
                        "The agent lost track of this operation (it probably restarted)", "operation_lost", 502
                    ) from exc
                raise
            if on_progress:
                on_progress(op)
            if op["status"] == "succeeded":
                return op
            if op["status"] == "failed":
                raise AgentError(op.get("error") or "Agent operation failed", op.get("error_code") or "operation_failed", 500)
            if time.monotonic() > deadline:
                raise AgentError("Timed out waiting for the agent operation", "operation_timeout", 504)
            time.sleep(interval)

    def ws_url(self, path: str) -> str:
        return self.base.replace("http://", "ws://").replace("https://", "wss://") + path
