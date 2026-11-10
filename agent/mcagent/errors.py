"""The agent's error type (re-exported by ``docker_ops``, where it used to live)."""

from __future__ import annotations


class AgentError(Exception):
    def __init__(self, message: str, code: str = "agent_error", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
