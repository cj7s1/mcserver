"""Tracking for long-running agent operations (image pulls, backups, restores, clones).

Operations live in memory: if the agent restarts mid-operation the management API
sees a 404 for the operation and reports the job as interrupted. Each instance can
run at most one operation at a time, which also serialises restic repository access.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .docker_ops import AgentError

log = logging.getLogger(__name__)

# Operation kinds that must not overlap with power actions on the same instance.
BLOCKS_LIFECYCLE = {"restore", "delete_data", "clone_target", "import", "apply"}


@dataclass
class Operation:
    kind: str
    instance_id: UUID | None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "pending"  # pending | running | succeeded | failed
    progress: float = 0.0
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=400))

    def update(self, progress: float | None = None, message: str | None = None) -> None:
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))
        if message:
            if message != self.message:
                self.log.append(f"{time.strftime('%H:%M:%S')} {message}")
            self.message = message

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "instance_id": str(self.instance_id) if self.instance_id else None,
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "error_code": self.error_code,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "log": list(self.log),
        }


class OperationRegistry:
    def __init__(self, retention_seconds: int = 6 * 3600) -> None:
        self._ops: dict[str, Operation] = {}
        self._active: dict[UUID, str] = {}
        self._lock = threading.Lock()
        self.retention = retention_seconds

    def _gc(self) -> None:
        cutoff = time.time() - self.retention
        for op_id in [k for k, op in self._ops.items() if op.finished_at and op.finished_at < cutoff]:
            del self._ops[op_id]

    def active_for(self, instance_id: UUID) -> Operation | None:
        with self._lock:
            op_id = self._active.get(instance_id)
            return self._ops.get(op_id) if op_id else None

    def assert_lifecycle_allowed(self, instance_id: UUID) -> None:
        op = self.active_for(instance_id)
        if op and op.kind in BLOCKS_LIFECYCLE:
            raise AgentError(f"Instance is busy with a '{op.kind}' operation", "instance_busy", 409)

    def start(
        self,
        kind: str,
        instance_ids: list[UUID],
        fn: Callable[[Operation], dict[str, Any] | None],
    ) -> Operation:
        primary = instance_ids[0] if instance_ids else None
        op = Operation(kind=kind, instance_id=primary)
        with self._lock:
            self._gc()
            for iid in instance_ids:
                busy = self._active.get(iid)
                if busy and self._ops.get(busy) and self._ops[busy].status in ("pending", "running"):
                    raise AgentError(
                        f"Instance {iid} is busy with operation '{self._ops[busy].kind}'",
                        "instance_busy",
                        409,
                    )
            self._ops[op.id] = op
            for iid in instance_ids:
                self._active[iid] = op.id

        def runner() -> None:
            op.status = "running"
            try:
                result = fn(op)
                op.result = result or {}
                op.progress = 1.0
                op.status = "succeeded"
            except AgentError as exc:
                op.status = "failed"
                op.error = str(exc)
                op.error_code = exc.code
                op.log.append(f"ERROR {exc}")
            except Exception as exc:
                log.exception("Operation %s (%s) crashed", op.id, kind)
                op.status = "failed"
                op.error = f"{type(exc).__name__}: {exc}"
                op.error_code = "internal_error"
                op.log.append("".join(traceback.format_exception_only(type(exc), exc)).strip())
            finally:
                op.finished_at = time.time()
                with self._lock:
                    for iid in instance_ids:
                        if self._active.get(iid) == op.id:
                            del self._active[iid]

        threading.Thread(target=runner, name=f"op-{kind}-{op.id[:8]}", daemon=True).start()
        return op

    def get(self, op_id: str) -> Operation | None:
        with self._lock:
            return self._ops.get(op_id)

    def list(self, instance_id: UUID | None = None) -> list[Operation]:
        with self._lock:
            ops = list(self._ops.values())
        if instance_id:
            ops = [o for o in ops if o.instance_id == instance_id]
        return sorted(ops, key=lambda o: o.created_at, reverse=True)


registry = OperationRegistry()
