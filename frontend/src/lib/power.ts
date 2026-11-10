/**
 * The one path every power request takes (POST /instances/<id>/power): the server header, the
 * library quick actions, the command palette, the Restart/Stop dialog and the "restart to apply"
 * notices all call `sendPower`.
 *
 * Memory admission: a start (or restart) may be refused with 409 `insufficient_memory` when the
 * servers already running plus this one would not fit into the host's memory; the message names the
 * numbers. `sendPower` then asks the user through the confirmation mounted by
 * `MemoryOvercommitHost` (components/MemoryOvercommitDialog.tsx) and, on "Start anyway", repeats
 * the same request with `allow_overcommit: true`. Without a mounted host, or from an older panel
 * that never answers with that code, the error simply reaches the caller as before.
 *
 * `postAllowingOvercommit` / `postOrDecline` ask the same for the other requests that start a
 * server through a job (create, software and modpack updates with a start, recreate, migrate).
 */
import { api, ApiError } from "./api";
import type { Instance } from "./types";

export const INSUFFICIENT_MEMORY = "insufficient_memory";

export function isInsufficientMemoryError(err: unknown): err is ApiError {
  return err instanceof ApiError && err.status === 409 && err.code === INSUFFICIENT_MEMORY;
}

export interface OvercommitRequest {
  instance: Pick<Instance, "id" | "name">;
  action: "start" | "restart";
  /** The backend's message (names the memory figures). */
  message: string;
}

type OvercommitAsker = (req: OvercommitRequest) => Promise<boolean>;

let asker: OvercommitAsker | null = null;

/** Register the confirmation UI (one per app); returns the unregister function. */
export function setOvercommitAsker(fn: OvercommitAsker): () => void {
  asker = fn;
  return () => {
    if (asker === fn) asker = null;
  };
}

export type PowerOutcome<T = unknown> = { status: "done"; data: T; overcommitted: boolean } | { status: "cancelled" };

export function powerPath(instanceId: string): string {
  return `/instances/${instanceId}/power`;
}

/**
 * POST the power request; on `insufficient_memory` ask whether to start anyway and repeat it with
 * `allow_overcommit: true`. Resolves `{status: "cancelled"}` when the user declines (nothing was
 * started, and there is nothing to report as an error); every other failure rejects.
 */
export async function sendPower<T = unknown>(
  instance: Pick<Instance, "id" | "name">,
  body: Record<string, unknown>,
): Promise<PowerOutcome<T>> {
  const action = body.action;
  if (action !== "start" && action !== "restart") {
    return { status: "done", data: await api.post<T>(powerPath(instance.id), body), overcommitted: false };
  }
  return postAllowingOvercommit<T>(powerPath(instance.id), instance, body, action);
}

/**
 * The same confirmation for the other requests whose job starts a server (backend
 * `capacity.admit_request`): creating a server that starts, a software or modpack update with
 * "start afterwards", recreating a running server's container, moving a running server.
 * `{status: "cancelled"}` when the user declines; other failures reject.
 */
export async function postAllowingOvercommit<T = unknown>(
  path: string,
  instance: Pick<Instance, "id" | "name">,
  body: Record<string, unknown>,
  action: OvercommitRequest["action"] = "start",
): Promise<PowerOutcome<T>> {
  try {
    return { status: "done", data: await api.post<T>(path, body), overcommitted: false };
  } catch (err) {
    if (!isInsufficientMemoryError(err) || body.allow_overcommit || !asker) throw err;
    const confirmed = await asker({ instance, action, message: err.message });
    if (!confirmed) return { status: "cancelled" };
    const data = await api.post<T>(path, { ...body, allow_overcommit: true });
    return { status: "done", data, overcommitted: true };
  }
}

/** Thrown by callers that need a value: the user declined "Start anyway?" (nothing was started). */
export class OvercommitDeclined extends Error {
  constructor() {
    super("Not started: not enough free memory");
    this.name = "OvercommitDeclined";
  }
}

/** `postAllowingOvercommit` for a mutation: the data, or OvercommitDeclined when the user said no. */
export async function postOrDecline<T = unknown>(
  path: string,
  instance: Pick<Instance, "id" | "name">,
  body: Record<string, unknown>,
  action: OvercommitRequest["action"] = "start",
): Promise<T> {
  const outcome = await postAllowingOvercommit<T>(path, instance, body, action);
  if (outcome.status === "cancelled") throw new OvercommitDeclined();
  return outcome.data;
}
