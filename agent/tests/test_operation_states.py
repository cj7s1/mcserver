"""R-4 of the review of the fix branch: an operation that already reports "succeeded" no longer blocks /start,
/restart and file writes in the moment before its runner clears it (registry.start judged it finished already).
Adopted from audit-2026-11-09/fixes/review/agent-fs/test_succeeded_op_still_blocks.py, turned around."""

from __future__ import annotations

import threading
import time
import types
from uuid import uuid4

import pytest

from mcagent import operations
from mcagent.docker_ops import AgentError


def test_a_finished_operation_does_not_block_while_it_is_cleared(monkeypatch):
    release = threading.Event()
    real_time = time.time

    def slow_time():
        if threading.current_thread().name.startswith("op-"):
            release.wait(2)  # the runner sits between `status = "succeeded"` and clearing _active
        return real_time()

    monkeypatch.setattr(operations, "time", types.SimpleNamespace(time=slow_time, strftime=time.strftime))
    reg = operations.OperationRegistry()
    iid = uuid4()
    op = reg.start("restore", [iid], lambda _op: {})
    try:
        for _ in range(200):
            if op.status == "succeeded":
                break
            time.sleep(0.005)
        assert op.status == "succeeded"
        reg.assert_lifecycle_allowed(iid)  # the panel's next /start or file write: allowed
    finally:
        release.set()


def test_a_running_operation_still_blocks():
    reg = operations.OperationRegistry()
    iid = uuid4()
    hold = threading.Event()
    reg.start("restore", [iid], lambda _op: hold.wait(5) and {})
    try:
        with pytest.raises(AgentError) as exc:
            reg.assert_lifecycle_allowed(iid)
        assert exc.value.code == "instance_busy"
    finally:
        hold.set()
