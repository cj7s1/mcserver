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
 * "Start anyway" is honoured only for users with the global `system.settings` permission (403
 * `overcommit_not_allowed` otherwise, D-4): the host offers it only to them (`mayOvercommit`).
 *
 * Missing secret: a start (or restart) of a server whose game needs a secret that is not set (Valheim's
 * join password) is refused with 409 `secret_required` (`details.key`, `details.label`). `sendPower`
 * then shows the notice mounted by `SecretRequiredHost` (components/SecretRequiredDialog.tsx), which links
 * to Settings -> Game settings -> Secrets, and resolves `{status: "cancelled"}` (nothing was started and
 * the notice said why). Without a mounted host the error reaches the caller as before.
 */
import { api, ApiError } from "./api";
import type { AuthState, Instance, PermissionCode } from "./types";

export const INSUFFICIENT_MEMORY = "insufficient_memory";
export const SECRET_REQUIRED = "secret_required";
/** The global permission the panel requires for `allow_overcommit` (backend `capacity.OVERCOMMIT_PERMISSION`). */
export const OVERCOMMIT_PERMISSION: PermissionCode = "system.settings";

export function isInsufficientMemoryError(err: unknown): err is ApiError {
  return err instanceof ApiError && err.status === 409 && err.code === INSUFFICIENT_MEMORY;
}

export function isSecretRequiredError(err: unknown): err is ApiError {
  return err instanceof ApiError && err.status === 409 && err.code === SECRET_REQUIRED;
}

/** Whether the signed-in user may start a server past the memory admission; null while the user is unknown. */
export function mayOvercommit(state: AuthState | undefined | null): boolean | null {
  const user = state?.user;
  if (!user) return null;
  return user.permissions.includes(OVERCOMMIT_PERMISSION);
}

/** Where a server's secrets are set: Settings -> Game settings (its Secrets section). */
export function secretsPath(instanceId: string): string {
  return `/servers/${instanceId}/settings?tab=game-settings`;
}

export interface SecretRequiredNotice {
  instance: Pick<Instance, "id" | "name">;
  action: "start" | "restart";
  /** The backend's message (names the secret). */
  message: string;
  /** The secret's label ("Join password") and key, when the answer names them. */
  label?: string;
  key?: string;
}

type SecretNotifier = (notice: SecretRequiredNotice) => void;

let secretNotifier: SecretNotifier | null = null;

/** Register the missing-secret notice (one per app); returns the unregister function. */
export function setSecretRequiredNotifier(fn: SecretNotifier): () => void {
  secretNotifier = fn;
  return () => {
    if (secretNotifier === fn) secretNotifier = null;
  };
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
  try {
    return await postAllowingOvercommit<T>(powerPath(instance.id), instance, body, action);
  } catch (err) {
    if (!isSecretRequiredError(err) || !secretNotifier) throw err;
    const details = err.details ?? {};
    secretNotifier({
      instance,
      action,
      message: err.message,
      label: typeof details.label === "string" && details.label ? details.label : undefined,
      key: typeof details.key === "string" && details.key ? details.key : undefined,
    });
    return { status: "cancelled" };
  }
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
