""".github/scripts/upgrade-rehearsal.sh speaks to the panel the way a browser tab does (security review D-1, I-7).

A panel that binds its sessions answers `session_token` with the login and wants it as
`X-MCPanel-Session-Token` on every `/api/` call; the rehearsal also talks to the old panel, which
answers none. The rehearsal's own `api` / `login` helpers run here against a stand-in for `curl` that
records what it was asked; the real run (containers) is the CI job "upgrade".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REHEARSAL = REPO / ".github" / "scripts" / "upgrade-rehearsal.sh"
ACCEPTANCE = REPO / ".github" / "scripts" / "panel-acceptance.sh"
TOKEN = "Ab3_-" + "x" * 38  # 43 characters of URL-safe base64

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("rev") is None, reason="needs bash")

# curl stand-in: one line per call in $CALLS ("METHOD URL | header | header ..."); writes the answer
# to the -o file and prints the status for -w.
FAKE_CURL = r"""#!/bin/bash
method=GET url="" out="" status=200 headers="" w=0
while [ $# -gt 0 ]; do
    case "$1" in
        -X) method=$2; shift ;;
        -o) out=$2; shift ;;
        -H) headers="$headers | $2"; shift ;;
        -w) w=1; shift ;;
        -b | -c | -d) shift ;;
        -*) ;;
        *) url=$1 ;;
    esac
    shift
done
printf '%s %s%s\n' "$method" "$url" "$headers" >>"$CALLS"
body='{"ok": true}'
case "$url" in
    */api/auth/login | */api/auth/setup)
        if [ -e "$NEW_PANEL" ]; then body="{\"id\":1,\"username\":\"owner\",\"session_token\":\"$TOKEN\"}"
        else body='{"id":1,"username":"owner"}'; fi ;;
esac
[ -n "$out" ] && [ "$out" != /dev/null ] && printf '%s' "$body" >"$out"
[ "$w" = 0 ] || printf '%s' "$status"
"""


def helpers() -> str:
    text = REHEARSAL.read_text()
    start = text.index("csrf() {")
    end = text.index("data_is_there() {")
    return text[start:end]


def run(tmp_path: Path, new_panel: bool, steps: str) -> tuple[str, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    calls = tmp_path / "calls.log"
    calls.unlink(missing_ok=True)
    flag = tmp_path / "new-panel"
    if new_panel:
        flag.touch()
    else:
        flag.unlink(missing_ok=True)
    script = f"""set -uo pipefail
URL=http://panel.test; OUT={tmp_path / "out"}; JAR={tmp_path / "jar"}; USERNAME=owner; PASSWORD=pw
touch "$JAR"
{helpers()}
{steps}
"""
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "CALLS": str(calls), "NEW_PANEL": str(flag), "TOKEN": TOKEN}
    done = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout, calls.read_text().splitlines()


STEPS = """login || exit 9
[ "$(api GET /api/system/settings)" = 200 ] || exit 8
[ "$(api PATCH /api/system/settings '{"values":{}}')" = 200 ] || exit 7
echo "token=$SESSION_TOKEN"
"""


def test_after_a_login_every_call_carries_the_session_token(tmp_path):
    out, calls = run(tmp_path, True, STEPS)
    assert f"token={TOKEN}" in out
    state, login, get, patch = calls
    assert state.startswith("GET http://panel.test/api/auth/state")
    assert login.startswith("POST http://panel.test/api/auth/login") and "X-MCPanel-Session-Token" not in login
    for call in (get, patch):
        assert f"| X-MCPanel-Session-Token: {TOKEN}" in call, call
        assert "| X-CSRFToken: " in call


def test_the_old_panel_gets_no_token_header(tmp_path):
    out, calls = run(tmp_path, False, STEPS)
    assert "token=\n" in out
    assert not [call for call in calls if "X-MCPanel-Session-Token" in call]


def test_setup_and_a_new_login_take_the_new_token(tmp_path):
    steps = """[ "$(api POST /api/auth/setup '{}')" = 200 ] || exit 9
remember_session_token
echo "after setup: $SESSION_TOKEN"
api GET /api/instances >/dev/null
SESSION_TOKEN=stale
login || exit 8
echo "after login: $SESSION_TOKEN"
"""
    out, calls = run(tmp_path, True, steps)
    assert f"after setup: {TOKEN}" in out and f"after login: {TOKEN}" in out
    assert f"| X-MCPanel-Session-Token: {TOKEN}" in calls[1]
    # a new login starts without the old token
    assert "stale" not in calls[3]


def test_every_check_of_the_data_signs_in_and_checks_that_the_cookie_alone_is_refused():
    text = REHEARSAL.read_text()
    body = text[text.index("data_is_there() {") :].split("\n}\n", 1)[0]
    assert "login ||" in body and "session_token_required" in body
    # the owner account is made with setup, whose answer carries the token as well
    after_setup = text[text.index('api POST /api/auth/setup "') :].split("\n", 2)[1]
    assert after_setup == "remember_session_token"


def test_the_acceptance_script_calls_no_route_that_needs_a_session():
    """It asks /api/health (exempt) and posts an oversized body to /api/auth/login (exempt)."""
    text = ACCEPTANCE.read_text()
    routes = {part.split("'")[0].split('"')[0].split(",")[0] for part in text.split("8000/api/")[1:]}
    assert routes == {"health", "auth/login"}
