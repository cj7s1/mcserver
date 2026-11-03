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
  return new ApiError(resp.status, code, message, details);
}

export interface RequestOptions {
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
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
  if (!resp.ok) throw await parseError(resp);
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
          new ApiError(
            xhr.status,
            body?.error?.code ?? `http_${xhr.status}`,
            body?.error?.message ?? "Upload failed",
            body?.error?.details ?? {},
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

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.display;
  if (err instanceof Error) return err.message;
  return String(err);
}
