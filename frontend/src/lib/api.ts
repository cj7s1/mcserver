/**
 * Typed fetch wrapper for the MCPanel API.
 *
 * - Uses the session cookie (same origin) and sends Django's CSRF token header
 *   on unsafe methods.
 * - Every non-2xx response becomes an ApiError carrying the backend's stable
 *   error code, message and details (see backend/apps/core/errors.py).
 */

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;
  /** Seconds from the answer's Retry-After header (429 answers), when it has one. */
  retryAfter?: number;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** Field errors from DRF validation responses, flattened to strings. */
  get fieldErrors(): Record<string, string> {
    const fields = (this.details?.fields ?? {}) as Record<string, unknown>;
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(fields)) {
      out[k] = Array.isArray(v) ? v.map(String).join(" ") : typeof v === "object" ? JSON.stringify(v) : String(v);
    }
    return out;
  }

  /** A single human-readable message including the first field error if present. */
  get display(): string {
    const fe = Object.entries(this.fieldErrors);
    if (this.code === "validation_error" && fe.length) {
      return fe.map(([k, v]) => (k === "non_field_errors" ? v : `${k.replace(/_/g, " ")}: ${v}`)).join(" · ");
    }
    return this.message;
  }
}

function getCookie(name: string): string | null {
  const match = document.cookie.split("; ").find((c) => c.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.split("=").slice(1).join("=")) : null;
}

export type Query = Record<string, string | number | boolean | undefined | null>;

function buildUrl(path: string, query?: Query): string {
  const url = path.startsWith("/api") ? path : `/api${path.startsWith("/") ? "" : "/"}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

type ApiErrorListener = (err: ApiError) => void;
const errorListeners = new Set<ApiErrorListener>();

/**
 * Observe every HTTP error response the client turns into an ApiError (e.g. to react to
 * `must_change_password` globally). Returns an unsubscribe function.
 */
export function onApiError(listener: ApiErrorListener): () => void {
  errorListeners.add(listener);
  return () => {
    errorListeners.delete(listener);
  };
}

function notifyApiError(err: ApiError): ApiError {
  for (const listener of errorListeners) {
    try {
      listener(err);
    } catch {
      /* a listener must never mask the original error */
    }
  }
  return err;
}

async function parseError(resp: Response): Promise<ApiError> {
  let code = `http_${resp.status}`;
  let message = resp.statusText || "Request failed";
  let details: Record<string, unknown> = {};
  try {
    const body = await resp.json();
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      details = body.error.details ?? {};
    } else if (body?.detail) {
      message = String(body.detail);
    }
  } catch {
    if (resp.status === 502 || resp.status === 503 || resp.status === 504) {
      message = "The panel API is unreachable. Is the server running?";
    }
  }
  const err = new ApiError(resp.status, code, message, details);
  const retryAfter = Number(resp.headers?.get?.("Retry-After") ?? NaN);
  if (resp.status === 429 && Number.isFinite(retryAfter) && retryAfter > 0) err.retryAfter = retryAfter;
  return err;
}

export interface RequestOptions {
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

let csrfPrimer: Promise<void> | null = null;

/**
 * Make sure Django's CSRF cookie exists before an unsafe request. Pages that never loaded /auth/state
 * (e.g. a password-reset link opened in a fresh browser) would otherwise fail with "CSRF cookie not set".
 */
export async function ensureCsrfCookie(): Promise<void> {
  if (getCookie("mcpanel_csrftoken")) return;
  csrfPrimer ??= fetch(buildUrl("/auth/state"), { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then(() => undefined)
    .catch(() => undefined)
    .finally(() => {
      csrfPrimer = null;
    });
  await csrfPrimer;
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...(opts.headers ?? {}) };
  let body: BodyInit | undefined;
  if (opts.body instanceof FormData) {
    body = opts.body;
  } else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    await ensureCsrfCookie();
    const token = getCookie("mcpanel_csrftoken");
    if (token) headers["X-CSRFToken"] = token;
  }
  let resp: Response;
  try {
    resp = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body,
      credentials: "same-origin",
      signal: opts.signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(0, "network_error", "Cannot reach the panel API. Check your connection.");
  }
  if (!resp.ok) throw notifyApiError(await parseError(resp));
  if (resp.status === 204) return undefined as T;
  const text = await resp.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) => request<T>("GET", path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Query) => request<T>("POST", path, { body, query }),
  put: <T>(path: string, body?: unknown, query?: Query) => request<T>("PUT", path, { body, query }),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, { body }),
  delete: <T>(path: string, body?: unknown, query?: Query) => request<T>("DELETE", path, { body, query }),
};

/** Upload files with progress reporting (XHR, because fetch has no upload progress). */
export function uploadWithProgress<T>(
  path: string,
  form: FormData,
  query: Query | undefined,
  onProgress?: (fraction: number) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", buildUrl(path, query));
    xhr.withCredentials = true;
    const token = getCookie("mcpanel_csrftoken");
    if (token) xhr.setRequestHeader("X-CSRFToken", token);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let body: any = undefined;
      try {
        body = xhr.responseText ? JSON.parse(xhr.responseText) : undefined;
      } catch {
        /* ignore */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as T);
      else
        reject(
          notifyApiError(
            new ApiError(
              xhr.status,
              body?.error?.code ?? `http_${xhr.status}`,
              body?.error?.message ?? "Upload failed",
              body?.error?.details ?? {},
            ),
          ),
        );
    };
    xhr.onerror = () => reject(new ApiError(0, "network_error", "Upload failed: network error"));
    xhr.send(form);
  });
}

export function downloadUrl(path: string, query?: Query): string {
  return buildUrl(path, query);
}

/**
 * Seconds until a request refused with 429 `throttled` may be tried again: `details.wait` (the per-account
 * limit on failed passwords, D-3), else the Retry-After header (the per-address limit); null for any other
 * error or when the answer names no wait.
 */
export function throttleWait(err: unknown): number | null {
  if (!(err instanceof ApiError) || err.status !== 429) return null;
  const wait = Number(err.details?.wait);
  if (Number.isFinite(wait) && wait > 0) return Math.ceil(wait);
  return err.retryAfter && err.retryAfter > 0 ? Math.ceil(err.retryAfter) : null;
}

function waitText(seconds: number): string {
  if (seconds < 60) return `${seconds} second${seconds === 1 ? "" : "s"}`;
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 120) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const hours = Math.round(minutes / 60);
  return `${hours} hours`;
}

/**
 * The message of a 429 `throttled` answer with its wait in words and the time it ends ("Try again in 9
 * minutes (at 14:32).") in place of the API's "Expected available in 540 seconds."; null for other errors.
 */
export function throttledMessage(err: unknown, now: number = Date.now()): string | null {
  if (!(err instanceof ApiError) || err.status !== 429) return null;
  const wait = throttleWait(err);
  const base = err.message.replace(/\s*Expected available in \d+ seconds?\.?\s*$/, "").trim() || "Too many attempts.";
  if (wait === null) return base;
  const at = new Date(now + wait * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${base}${/[.!?]$/.test(base) ? "" : "."} Try again in ${waitText(wait)} (at ${at}).`;
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.display;
  if (err instanceof Error) return err.message;
  return String(err);
}
