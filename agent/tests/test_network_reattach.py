"""Starting a stopped server after its network was removed and created again.

Docker allows removing a network that only stopped containers use (``docker compose
down`` with every server stopped, ``docker network prune``). Containers that ran before
keep the old network ID; ``docker_ops.start`` must reattach them by name. Uses a real
Docker daemon and a tiny image (``MCPANEL_TEST_TINY_IMAGE``, default busybox).
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from mcagent import docker_ops

pytestmark = pytest.mark.docker

TINY_IMAGE = os.environ.get("MCPANEL_TEST_TINY_IMAGE", "busybox:1.37")


@pytest.fixture
def docker_client():
    import docker

    try:
        client = docker.from_env(timeout=60)
        client.ping()
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"Docker daemon not reachable: {exc}")
    try:
        client.images.get(TINY_IMAGE)
    except docker.errors.ImageNotFound:
        try:
            client.images.pull(TINY_IMAGE)
        except docker.errors.APIError as exc:  # pragma: no cover - registry rate limits
            pytest.skip(f"cannot pull {TINY_IMAGE}: {exc}")
    yield client
    client.close()


def _wait_exited(container, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while True:
        container.reload()
        if container.status == "exited":
            return
        assert time.monotonic() < deadline, f"container did not exit (status {container.status})"
        time.sleep(0.2)


def test_start_reattaches_container_to_recreated_network(docker_client, monkeypatch):
    monkeypatch.setattr(docker_ops, "client", lambda: docker_client)
    iid = uuid.uuid4()
    net_name = f"mcpanel-test-{uuid.uuid4().hex[:10]}"
    network = docker_client.networks.create(net_name, driver="bridge")
    container = None
    try:
        container = docker_client.containers.create(
            TINY_IMAGE,
            ["sleep", "1"],
            name=docker_ops.container_name(iid),
            labels={docker_ops.LABEL_MANAGED: "true", docker_ops.LABEL_INSTANCE: str(iid)},
            network=net_name,
        )
        container.start()
        _wait_exited(container)

        # What `docker compose down` / `docker network prune` do while the server is stopped.
        old_id = network.id
        network.remove()
        network = docker_client.networks.create(net_name, driver="bridge")
        assert network.id != old_id

        docker_ops.start(iid)

        container.reload()
        attached = container.attrs["NetworkSettings"]["Networks"]
        assert list(attached) == [net_name]
        assert attached[net_name]["NetworkID"] == network.id
        # Already attached to the current network: nothing to repair on the next start.
        _wait_exited(container)
        assert docker_ops.reattach_recreated_networks(container) == []
    finally:
        if container is not None:
            container.remove(force=True)
        network.remove()
