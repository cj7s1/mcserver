from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, WebSocket, status

from .config import Settings, get_settings


def _check(header: str | None, settings: Settings) -> None:
    expected = settings.resolve_token()
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Agent token is not configured")
    if not header or not header.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    if not hmac.compare_digest(header[7:].encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid bearer token")


def require_token(request: Request, settings: Settings = Depends(get_settings)) -> None:
    _check(request.headers.get("authorization"), settings)


async def websocket_authorized(websocket: WebSocket, settings: Settings) -> bool:
    try:
        _check(websocket.headers.get("authorization"), settings)
    except HTTPException:
        await websocket.close(code=4401)
        return False
    return True
