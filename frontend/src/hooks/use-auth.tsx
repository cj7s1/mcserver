import { hashKey, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { api, onApiError } from "@/lib/api";
import { isMustChangePasswordError, markMustChangePassword, resolveAuthState, sessionUserId } from "@/lib/auth-gate";
import { qk, useAuthState } from "@/lib/queries";
import type { AuthState, Me, PermissionCode } from "@/lib/types";
import { clearUserStorage, syncStorageOwner } from "@/lib/user-storage";

/** Same key (and endpoint) as the Account page's profile query. */
const meKey = [...qk.auth, "me"] as const;

export function useAuth() {
  const auth = useAuthState();
  const qc = useQueryClient();
  // /auth/state may itself be refused while a password change is pending; /auth/me never is.
  const stateBlocked = isMustChangePasswordError(auth.error);
  const me = useQuery({ queryKey: meKey, queryFn: () => api.get<Me>("/auth/me"), enabled: stateBlocked });
  const state = resolveAuthState(auth.data, stateBlocked, me.data);
  const user: Me | null = state?.user ?? null;
  return {
    state,
    user,
    isLoading: auth.isLoading || (stateBlocked && me.isLoading),
    error: stateBlocked ? me.error : auth.error,
    refetch: auth.refetch,
    can: (code: PermissionCode) => !!user?.permissions.includes(code),
    logout: async () => {
      clearUserStorage(); // first, so this tab's history never outlives the click (even if the request fails)
      await api.post("/auth/logout");
      // Drop every cached response, then tell the mounted auth gate directly (observers of removed
      // queries are not notified) and confirm with the server.
      const authHash = hashKey(qk.auth);
      qc.removeQueries({ predicate: (q) => q.queryHash !== authHash });
      qc.setQueryData<AuthState>(qk.auth, (old) => ({ setup_required: old?.setup_required ?? false, authenticated: false, user: null }));
      await qc.invalidateQueries({ queryKey: qk.auth });
    },
  };
}

/**
 * Session-wide side effects; mount once (the router's auth gate):
 * - when the signed-in user changes or signs out, browser storage holding user data is wiped and
 *   the previous account's cached responses are dropped (the gate also remounts the pages, keyed
 *   by user id), before anything renders for the new user;
 * - a 403 `must_change_password` from any request flags the cached user so the gate shows the
 *   "Set a new password" screen immediately.
 */
export function useAuthLifecycle(state: AuthState | undefined) {
  const qc = useQueryClient();
  const userId = sessionUserId(state);

  // Covers every source of the resolved state (incl. the /auth/me fallback); idempotent.
  useEffect(() => {
    if (userId !== undefined) syncStorageOwner(userId);
  }, [userId]);

  // Cache listeners run synchronously when the auth query's data changes, i.e. before React
  // re-renders the pages for the new user, so they never see the previous user's data.
  useEffect(() => {
    const authHash = hashKey(qk.auth);
    let known = sessionUserId(qc.getQueryData<AuthState>(qk.auth));
    return qc.getQueryCache().subscribe((event) => {
      if (event.type !== "updated" || event.query.queryHash !== authHash) return;
      const id = sessionUserId(event.query.state.data as AuthState | undefined);
      if (id === undefined || id === known) return;
      const previous = known;
      known = id;
      syncStorageOwner(id);
      if (previous !== undefined) qc.removeQueries({ predicate: (q) => q.queryHash !== authHash });
    });
  }, [qc]);

  useEffect(
    () =>
      onApiError((err) => {
        if (!isMustChangePasswordError(err)) return;
        const current = qc.getQueryData<AuthState>(qk.auth);
        const next = markMustChangePassword(current);
        if (next === current) return; // already flagged, or no session yet: nothing to do (and no refetch loop)
        qc.setQueryData(qk.auth, next);
        // Confirm with the server (a stale in-flight request may have raced a completed change).
        void qc.invalidateQueries({ queryKey: qk.auth });
      }),
    [qc],
  );
}
