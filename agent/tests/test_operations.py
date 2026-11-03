"""OperationRegistry: background operations, per-instance exclusivity, failure reporting."""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from mcagent.docker_ops import AgentError
from mcagent.operations import OperationRegistry


def wait(op, timeout=5):
    deadline = time.monotonic() + timeout
    while op.status in ("pending", "running"):
        assert time.monotonic() < deadline, "operation did not finish"
        time.sleep(0.01)
    return op


def test_success_and_progress():
    reg = OperationRegistry()
    iid = uuid.uuid4()

    def work(op):
        op.update(0.5, "half")
        return {"ok": True}

    op = wait(reg.start("backup", [iid], work))
    assert op.status == "succeeded" and op.result == {"ok": True} and op.progress == 1.0
    assert any("half" in line for line in op.log)
    assert reg.active_for(iid) is None
    assert reg.get(op.id) is op
    assert reg.list(iid) == [op]
    assert op.as_dict()["instance_id"] == str(iid)


def test_agent_error_and_crash_are_reported():
    reg = OperationRegistry()

    def fails(op):
        raise AgentError("restic exploded", "restic_failed", 500)

    def crashes(op):
        raise ZeroDivisionError("oops")

    op = wait(reg.start("backup", [uuid.uuid4()], fails))
    assert (op.status, op.error, op.error_code) == ("failed", "restic exploded", "restic_failed")
    op = wait(reg.start("backup", [uuid.uuid4()], crashes))
    assert op.status == "failed" and op.error_code == "internal_error" and "ZeroDivisionError" in op.error


def test_one_operation_per_instance_and_lifecycle_blocking():
    reg = OperationRegistry()
    a, b = uuid.uuid4(), uuid.uuid4()
    release = threading.Event()
    op = reg.start("restore", [a, b], lambda _op: release.wait(5) and {})
    try:
        for iid in (a, b):
            with pytest.raises(AgentError) as exc:
                reg.start("backup", [iid], lambda _op: {})
            assert exc.value.code == "instance_busy" and exc.value.status == 409
            with pytest.raises(AgentError):
                reg.assert_lifecycle_allowed(iid)  # restore blocks power actions
        reg.assert_lifecycle_allowed(uuid.uuid4())
    finally:
        release.set()
    wait(op)
    reg.assert_lifecycle_allowed(a)
    wait(reg.start("backup", [a], lambda _op: {}))


def test_backup_does_not_block_power_actions():
    reg = OperationRegistry()
    iid = uuid.uuid4()
    release = threading.Event()
    op = reg.start("backup", [iid], lambda _op: release.wait(5) and {})
    try:
        reg.assert_lifecycle_allowed(iid)
    finally:
        release.set()
        wait(op)


def test_finished_operations_are_garbage_collected():
    reg = OperationRegistry(retention_seconds=0)
    old = wait(reg.start("check", [uuid.uuid4()], lambda _op: {}))
    old.finished_at = time.time() - 10
    wait(reg.start("check", [uuid.uuid4()], lambda _op: {}))
    assert reg.get(old.id) is None
