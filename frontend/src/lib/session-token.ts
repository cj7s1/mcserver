/**
 * The session token of this browser (D-1 / I-7 of the security review 2026-11-11).
 *
 * A cookie belongs to a host name, not to a port, so the panel's session cookie also reaches every other
 * port of the host (game servers, other web UIs). The cookie alone is therefore no sign-in: the login and
 * first-run setup answers carry a `session_token` (shown once), and every `/api/` request must send it in
 * `X-MCPanel-Session-Token` (`lib/api.ts`), every WebSocket as its first frame (`sendAuthFrame`), and every
 * link download gets a short ticket for its URL instead (`lib/downloads.ts`).
 *
 * The token lives in `localStorage`, which is scoped to the panel's scheme, host and port (a page on
 * another port cannot read it), under one key. A new sign-in replaces it; it is removed at logout and when
 * the panel says it is not (or no longer) the session's token (401 `session_token_required`,
 * `auth/state` with `session_token_required: true`). It never goes into a URL, a log line or an error.
 * When storage is unavailable (blocked, private mode) it is kept in memory for this tab only.
 */

export const SESSION_TOKEN_KEY = "mcpanel.session.token";
export const SESSION_TOKEN_HEADER = "X-MCPanel-Session-Token";
export const SESSION_TOKEN_REQUIRED = "session_token_required";

let memory: string | null = null;

export function getSessionToken(): string | null {
  try {
    return localStorage.getItem(SESSION_TOKEN_KEY) || memory;
  } catch {
    return memory; // storage unavailable: this tab's copy
  }
}

/** Keep the token a sign-in answered with (a non-text or empty value changes nothing). */
export function setSessionToken(token: unknown): void {
  if (typeof token !== "string" || !token) return;
  try {
    localStorage.setItem(SESSION_TOKEN_KEY, token);
    memory = null;
  } catch {
    memory = token; // storage unavailable or full: this tab keeps it in memory
  }
}

/**
 * Forget the token. With `onlyIf`, only when the stored token is still that one: an answer to a request
 * sent with an older token must not remove the token a newer sign-in (e.g. in another tab) stored meanwhile.
 */
export function clearSessionToken(onlyIf?: string | null): void {
  if (onlyIf !== undefined && getSessionToken() !== onlyIf) return;
  memory = null;
  try {
    localStorage.removeItem(SESSION_TOKEN_KEY);
  } catch {
    /* storage unavailable */
  }
}

/** The header to send with an `/api/` request: empty while there is no token. */
export function sessionTokenHeaders(token: string | null = getSessionToken()): Record<string, string> {
  return token ? { [SESSION_TOKEN_HEADER]: token } : {};
}

const authedSockets = new WeakSet<WebSocket>();
const acceptedSockets = new WeakSet<WebSocket>();

/**
 * The first frame of every panel WebSocket (console, live events): `{"type": "auth", "token": ...}`. The
 * panel sends nothing and takes no other frame before it, and closes the socket with 4401 when it is
 * missing or wrong. Call it first thing in `onopen`.
 */
export function sendAuthFrame(sock: WebSocket): void {
  sock.send(JSON.stringify({ type: "auth", token: getSessionToken() ?? "" }));
  authedSockets.add(sock);
}

/** Whether `sock` already sent its auth frame. */
export function socketAuthenticated(sock: WebSocket | null | undefined): boolean {
  return !!sock && authedSockets.has(sock);
}

/**
 * Call for every frame the panel sends. The panel answers a good auth frame with `{"type": "auth_ok"}` as
 * its first frame; a panel that does not send it yet starts with its usual frames, so any first frame
 * after the auth frame accepts the socket. Returns true for `auth_ok` itself, which carries nothing else
 * (the caller skips it).
 */
export function noteSocketFrame(sock: WebSocket, msg: unknown): boolean {
  if (authedSockets.has(sock)) acceptedSockets.add(sock);
  return !!msg && typeof msg === "object" && (msg as { type?: unknown }).type === "auth_ok";
}

/** Whether the panel accepted `sock`'s auth frame (only then are the page's own frames sent). */
export function socketAccepted(sock: WebSocket | null | undefined): boolean {
  return !!sock && acceptedSockets.has(sock);
}
