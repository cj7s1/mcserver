/**
 * Pure decision logic for the authentication gate in `router.tsx`.
 *
 * While an account has `must_change_password` set, the backend answers every API endpoint except
 * auth/me, auth/logout and auth/password with 403 `must_change_password`. The SPA then renders a
 * focused "Set a new password" screen in place of every page (the URL is kept, so the original
 * page shows again once the password has been changed).
 */
import { ApiError } from "@/lib/api";
import type { AuthState, Me } from "@/lib/types";

export const MUST_CHANGE_PASSWORD = "must_change_password";

export function isMustChangePasswordError(err: unknown): err is ApiError {
  return err instanceof ApiError && err.status === 403 && err.code === MUST_CHANGE_PASSWORD;
}

/** `state` with the signed-in user flagged as having to change the password (same object if nothing changes). */
export function markMustChangePassword(state: AuthState | undefined): AuthState | undefined {
  if (!state?.authenticated || !state.user || state.user.must_change_password) return state;
  return { ...state, user: { ...state.user, must_change_password: true } };
}

/**
 * The auth state to use. `stateBlocked` means /auth/state itself was refused with
 * `must_change_password`; the user is then taken from /auth/me (`me`), or the last known state.
 */
export function resolveAuthState(
  state: AuthState | undefined,
  stateBlocked: boolean,
  me: Me | undefined,
): AuthState | undefined {
  if (!stateBlocked) return state;
  if (me) return { setup_required: false, authenticated: true, user: { ...me, must_change_password: true } };
  return markMustChangePassword(state);
}

/** Id of the signed-in user; null when nobody is signed in; undefined while unknown. */
export function sessionUserId(state: AuthState | undefined): number | null | undefined {
  if (state === undefined) return undefined;
  return state.authenticated && state.user ? state.user.id : null;
}

/** True when the cached auth state is shown although its last refetch failed (connection lost). */
export function authStateStale({ state, error }: { state: AuthState | undefined; error: unknown }): boolean {
  return !!error && !!state && !isMustChangePasswordError(error);
}

export type AuthGateView = "loading" | "error" | "setup" | "login" | "change-password" | "app";

export function authGateView({
  state,
  isLoading,
}: {
  state: AuthState | undefined;
  isLoading: boolean;
  /** Not used for the decision any more (see below); `authStateStale` tells whether to show a banner. */
  error: unknown;
}): AuthGateView {
  if (isLoading) return "loading";
  if (state?.authenticated && state.user?.must_change_password) return "change-password";
  // A failed background refetch keeps the cached state: only fail closed when there is none, so the
  // pages (and their unsaved drafts) stay mounted while the panel is briefly unreachable. Real
  // sign-outs arrive as a 200 with authenticated:false (or a 4401 socket close), not as an error.
  if (!state) return "error";
  if (state.setup_required) return "setup";
  if (!state.authenticated || !state.user) return "login";
  return "app";
}
