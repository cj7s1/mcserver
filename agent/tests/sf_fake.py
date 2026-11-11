"""A fake Satisfactory dedicated server for the agent's tests: the HTTPS API (self-signed TLS, the
spike's recorded requests and answers, errors as HTTP 200 + ``errorCode``) and the UDP Lightweight
Query on the same port number. State: claimed or not, admin/client password, API tokens, sessions
and saves, so the claim flow and every failure point can be played through.

Answers copied from audit-2026-11-09/design/spike-results.md section 5.5 (tokens are made up here,
in the same ``<base64 JSON>.<hex>`` shape).
"""

from __future__ import annotations

import base64
import json
import secrets
import socket
import ssl
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def make_token(level: str) -> str:
    payload = base64.b64encode(json.dumps({"pl": level}).encode()).decode().rstrip("=")
    return f"{payload}.{secrets.token_hex(16)}"


def level_of(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    raw = token.split(".", 1)[0]
    raw += "=" * (-len(raw) % 4)
    try:
        return json.loads(base64.b64decode(raw))["pl"]
    except Exception:
        return None


def error(code: str, message: str, status: int = 200, **extra: Any) -> tuple[int, dict[str, Any]]:
    return status, {"errorCode": code, "errorMessage": message, **extra}


class FakeSatisfactory:
    def __init__(self, cert_dir: Path) -> None:
        from mcagent import tls

        cert, key = cert_dir / tls.CERT_NAME, cert_dir / tls.KEY_NAME
        if not cert.exists():
            cert, key = tls.generate_self_signed(cert_dir, ["127.0.0.1"])
        self.cert_file, self.key_file = cert, key
        self.claimed = False
        self.server_name = ""
        self.admin_password: str | None = None
        self.client_password = ""
        self.tokens: dict[str, str] = {}  # token -> level
        self.sessions: dict[str, list[dict[str, Any]]] = {}
        self.active_session = ""
        self.players = 0
        self.player_limit = 4
        self.options = {
            "FG.WeatherPreset": "0",
            "FG.DSAutoPause": "True",
            "FG.DSAutoSaveOnDisconnect": "True",
            "FG.AgreeToCrashUpload": "False",
            "FG.AutosaveInterval": "300.0",
            "FG.EnableSeasonalEvents": "True",
            "FG.NetworkQuality": "1",
            "FG.SendGameplayData": "False",
            "FG.ServerRestartTimeSlot": "1440.0",
        }
        self.requests: list[tuple[str, dict[str, Any], str | None]] = []  # (function, data, token level)
        self.uploads: list[tuple[str, bytes, dict[str, Any]]] = []
        self.shutdown_called = threading.Event()
        self.health = "healthy"
        self.api_down = False  # answers HTTP 503 (loading)
        self.fail: dict[str, tuple[int, dict[str, Any]]] = {}  # function -> forced answer
        self.no_token_line = False
        self.udp_state = 1
        self.udp_answers = True
        self.on_shutdown: Any = None
        self._start()

    # --- servers -------------------------------------------------------------------------------
    def _start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:  # quiet
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                auth = self.headers.get("Authorization") or ""
                token = auth[7:] if auth.startswith("Bearer ") else None
                status, body = fake.handle(self.path, self.headers.get("Content-Type") or "", raw, token)
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert_file, self.key_file)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", self.port))
        self.udp.settimeout(0.2)
        self._stop = threading.Event()
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        threading.Thread(target=self._udp_loop, daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.udp.close()

    def _udp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, peer = self.udp.recvfrom(64)
            except (TimeoutError, OSError):
                continue
            if not self.udp_answers or len(data) != 13:
                continue
            magic, kind, proto, cookie, term = struct.unpack("<HBBQB", data)
            if magic != 0xF6D5 or kind != 0 or term != 1:
                continue
            name = self.server_name.encode()
            reply = struct.pack("<HBBQBIQB", 0xF6D5, 1, proto, cookie, self.udp_state, 517554, 0, 4)
            reply += struct.pack("<BHBHBHBH", 0, 3, 1, 1, 3, 1, 2, 1)
            reply += struct.pack("<H", len(name)) + name + b"\x01"
            try:
                self.udp.sendto(reply, peer)
            except OSError:
                pass

    # --- the API -------------------------------------------------------------------------------
    def handle(
        self, path: str, ctype: str, raw: bytes, token: str | None
    ) -> tuple[int, dict[str, Any] | None]:
        if path != "/api/v1":
            return 404, {"errorCode": "not_found"}
        if self.api_down:
            return 503, None
        files: dict[str, bytes] = {}
        if ctype.startswith("multipart/form-data"):
            boundary = ctype.split("boundary=", 1)[1].encode()
            request: dict[str, Any] = {}
            for part in raw.split(b"--" + boundary):
                if b"\r\n\r\n" not in part:
                    continue
                head, _, content = part.partition(b"\r\n\r\n")
                content = content.removesuffix(b"\r\n")
                if b'name="data"' in head:
                    request = json.loads(content)
                elif b'name="saveGameFile"' in head:
                    files["saveGameFile"] = content
        else:
            try:
                request = json.loads(raw or b"{}")
            except ValueError:
                return 400, {"errorCode": "invalid_json"}
        function = request.get("function", "")
        data = request.get("data") or {}
        level = None
        if token is not None:
            level = self.tokens.get(token)
            if level is None:
                return 401, {"errorCode": "invalid_token", "errorMessage": "The provided token is invalid"}
        self.requests.append((function, data, level))
        if function in self.fail:
            return self.fail[function]
        handler = getattr(self, f"f_{function}", None)
        if handler is None:
            return error("unknown_function", f"Unknown function {function}")
        return handler(data, level, files)

    def _need(self, level: str | None, *allowed: str) -> tuple[int, dict[str, Any]] | None:
        if level not in allowed:
            return error(
                "insufficient_scope",
                "The client is missing required privileges to access the given function",
                403,
            )
        return None

    def _token(self, level: str) -> str:
        token = make_token(level)
        self.tokens[token] = level
        return token

    def f_HealthCheck(self, data, level, files):
        return 200, {"data": {"health": self.health, "serverCustomData": ""}}

    def f_PasswordlessLogin(self, data, level, files):
        wanted = data.get("MinimumPrivilegeLevel")
        if wanted == "InitialAdmin" and not self.claimed:
            return 200, {"data": {"authenticationToken": self._token("InitialAdmin")}}
        if wanted == "Client" and self.claimed and not self.client_password:
            return 200, {"data": {"authenticationToken": self._token("Client")}}
        return error(
            "passwordless_login_not_possible",
            "Passwordless login is not possible in the current server configuration",
        )

    def f_PasswordLogin(self, data, level, files):
        if data.get("Password") == self.admin_password and self.claimed:
            return 200, {"data": {"authenticationToken": self._token("Administrator")}}
        if self.client_password and data.get("Password") == self.client_password:
            return 200, {"data": {"authenticationToken": self._token("Client")}}
        return error("wrong_password", "Provided password did not match either Client or Admin passwords")

    def f_ClaimServer(self, data, level, files):
        if self.claimed:
            return error(
                "server_claimed",
                "Server is already claimed. To rename it or set admin password, use RenameServer or SetAdminPassword.",
            )
        if (denied := self._need(level, "InitialAdmin")) is not None:
            return denied
        self.claimed = True
        self.server_name = data["ServerName"]
        self.admin_password = data["AdminPassword"]
        return 200, {"data": {"authenticationToken": self._token("Administrator")}}

    def f_RunCommand(self, data, level, files):
        if (denied := self._need(level, "Administrator")) is not None:
            return denied
        if data.get("Command") == "server.GenerateAPIToken":
            text = (
                "No token for you"
                if self.no_token_line
                else f"New Server API Authentication Token: {self._token('APIToken')}"
            )
            return 200, {"data": {"commandResult": text, "returnValue": True}}
        return 200, {"data": {"commandResult": "", "returnValue": False}}

    def f_VerifyAuthenticationToken(self, data, level, files):
        if level is None:
            return error(
                "missing_params", "missing", errorData={"missingParameters": ["authenticationToken"]}
            )
        return 204, None

    def f_QueryServerState(self, data, level, files):
        if (denied := self._need(level, "Client", "Administrator", "APIToken")) is not None:
            return denied
        return 200, {
            "data": {
                "serverGameState": {
                    "activeSessionName": self.active_session,
                    "numConnectedPlayers": self.players,
                    "playerLimit": self.player_limit,
                    "techTier": 0,
                    "activeSchematic": "None",
                    "gamePhase": "None",
                    "isGameRunning": bool(self.active_session),
                    "totalGameDuration": 0,
                    "isGamePaused": False,
                    "averageTickRate": 29.95,
                    "autoLoadSessionName": self.active_session,
                    "hasAskedForDataSharingConsent": False,
                }
            }
        }

    def f_SetClientPassword(self, data, level, files):
        if (denied := self._need(level, "Administrator", "APIToken")) is not None:
            return denied
        self.client_password = data.get("Password", "")
        return 204, None

    def f_CreateNewGame(self, data, level, files):
        if (denied := self._need(level, "Administrator", "APIToken")) is not None:
            return denied
        new = data.get("NewGameData") or {}
        if "GameModeSettings" not in new:
            return error(
                "missing_params",
                "",
                errorData={
                    "missingParameters": [],
                    "invalidParameters": {
                        "newGameData": "Unable to import JSON object into FGServerNewGameData property NewGameData\n"
                        "Missing JSON value named GameModeSettings"
                    },
                },
            )
        session = new["SessionName"]
        self.active_session = session
        self.udp_state = 3
        self.sessions.setdefault(session, []).insert(
            0, {"saveName": f"{session}_autosave_0", "sessionName": session}
        )
        return 202, None

    def f_SaveGame(self, data, level, files):
        if (denied := self._need(level, "Administrator", "APIToken")) is not None:
            return denied
        name = data["SaveName"]
        saves = self.sessions.setdefault(self.active_session, [])
        saves[:] = [s for s in saves if s["saveName"] != name]
        saves.insert(0, {"saveName": name, "sessionName": self.active_session})
        return 204, None

    def f_EnumerateSessions(self, data, level, files):
        if (denied := self._need(level, "Administrator", "APIToken")) is not None:
            return denied
        names = list(self.sessions)
        sessions = [
            {
                "sessionName": n,
                "saveHeaders": [
                    {
                        "saveVersion": 60,
                        "buildVersion": 517554,
                        "saveName": s["saveName"],
                        "saveLocationInfo": "SLI_Server",
                        "mapName": "Persistent_Level",
                        "mapOptions": "?skiponboarding",
                        "sessionName": n,
                        "playDurationSeconds": 13,
                        "saveDateTime": "2026.11.10-23.28.30",
                        "isModdedSave": False,
                        "isEditedSave": False,
                        "isCreativeModeEnabled": False,
                    }
                    for s in self.sessions[n]
                ],
            }
            for n in names
        ]
        current = names.index(self.active_session) if self.active_session in names else -1
        return 200, {"data": {"sessions": sessions, "currentSessionIndex": current}}

    def f_UploadSaveGame(self, data, level, files):
        if (denied := self._need(level, "Administrator", "APIToken")) is not None:
            return denied
        content = files.get("saveGameFile")
        if content is None:
            return error("missing_params", "saveGameFile")
        self.uploads.append((data["SaveName"], content, data))
        session = "imported-session"
        self.sessions.setdefault(session, []).insert(
            0, {"saveName": data["SaveName"], "sessionName": session}
        )
        if data.get("LoadSaveGame"):
            self.active_session = session
            self.udp_state = 3
        return 202, None

    def f_LoadGame(self, data, level, files):
        if (denied := self._need(level, "Administrator", "APIToken")) is not None:
            return denied
        for session, saves in self.sessions.items():
            if any(s["saveName"] == data["SaveName"] for s in saves):
                self.active_session = session
                return 202, None
        return error("save_game_load_failed", "no such save")

    def f_GetServerOptions(self, data, level, files):
        if (denied := self._need(level, "Administrator", "APIToken")) is not None:
            return denied
        return 200, {"data": {"serverOptions": dict(self.options), "pendingServerOptions": {}}}

    def f_ApplyServerOptions(self, data, level, files):
        if (denied := self._need(level, "Administrator", "APIToken")) is not None:
            return denied
        self.options.update(data.get("UpdatedServerOptions") or {})
        return 204, None

    def f_Shutdown(self, data, level, files):
        if (denied := self._need(level, "Administrator", "APIToken")) is not None:
            return denied
        self.shutdown_called.set()
        if self.on_shutdown is not None:
            self.on_shutdown()
        return 204, None

    def functions(self) -> list[str]:
        return [f for f, _d, _l in self.requests]
