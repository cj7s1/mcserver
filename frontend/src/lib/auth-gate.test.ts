import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, onApiError } from "./api";
import { authGateView, authStateStale, isMustChangePasswordError, markMustChangePassword, resolveAuthState, sessionUserId } from "./auth-gate";
import type { AuthState, Me } from "./types";

const me = (over: Partial<Me> = {}): Me => ({
  id: 4,
  username: "alice",
  email: "",
  first_name: "",
  last_name: "",
  is_owner: false,
  role_name: null,
  permissions: [],
  must_change_password: false,
  preferences: {},
  ...over,
});
const signedIn = (over: Partial<Me> = {}): AuthState => ({ setup_required: false, authenticated: true, user: me(over) });
const mustChange = new ApiError(403, "must_change_password", "You must change your password first.");

afterEach(() => vi.unstubAllGlobals());

describe("must_change_password detection", () => {
  it("matches only a 403 with the stable code", () => {
    expect(isMustChangePasswordError(mustChange)).toBe(true);
    expect(isMustChangePasswordError(new ApiError(403, "permission_denied", "no"))).toBe(false);
    expect(isMustChangePasswordError(new ApiError(400, "must_change_password", "?"))).toBe(false);
    expect(isMustChangePasswordError(new Error("must_change_password"))).toBe(false);
    expect(isMustChangePasswordError(null)).toBe(false);
  });

  it("flags the signed-in user without touching anonymous or already-flagged state", () => {
    const s = signedIn();
    const flagged = markMustChangePassword(s)!;
    expect(flagged.user!.must_change_password).toBe(true);
    expect(s.user!.must_change_password).toBe(false); // not mutated
    expect(markMustChangePassword(flagged)).toBe(flagged);
    const anon: AuthState = { setup_required: false, authenticated: false, user: null };
    expect(markMustChangePassword(anon)).toBe(anon);
    expect(markMustChangePassword(undefined)).toBeUndefined();
  });

  it("falls back to /auth/me when /auth/state itself is refused", () => {
    const stale = signedIn();
    expect(resolveAuthState(stale, false, undefined)).toBe(stale);
    expect(resolveAuthState(stale, true, undefined)!.user!.must_change_password).toBe(true);
    const fromMe = resolveAuthState(undefined, true, me({ username: "bob" }))!;
    expect(fromMe).toMatchObject({ setup_required: false, authenticated: true });
    expect(fromMe.user).toMatchObject({ username: "bob", must_change_password: true });
    expect(resolveAuthState(undefined, true, undefined)).toBeUndefined();
  });
});

describe("session user id", () => {
  it("distinguishes unknown, signed out and signed in", () => {
    expect(sessionUserId(undefined)).toBeUndefined();
    expect(sessionUserId({ setup_required: false, authenticated: false, user: null })).toBeNull();
    expect(sessionUserId(signedIn())).toBe(4);
  });
});

describe("auth gate decision", () => {
  const view = (state: AuthState | undefined, extra: { isLoading?: boolean; error?: unknown } = {}) =>
    authGateView({ state, isLoading: extra.isLoading ?? false, error: extra.error ?? null });

  it("routes a flagged user to the password screen on every page", () => {
    expect(view(signedIn({ must_change_password: true }))).toBe("change-password");
    // even when a refetch was refused with must_change_password
    expect(view(signedIn({ must_change_password: true }), { error: mustChange })).toBe("change-password");
  });

  it("keeps the existing gate behaviour otherwise", () => {
    expect(view(undefined, { isLoading: true })).toBe("loading");
    expect(view(undefined, { error: new Error("down") })).toBe("error");
    // a failed background refetch keeps the app (and its unsaved page state) mounted
    expect(view(signedIn(), { error: new Error("down") })).toBe("app");
    expect(view({ setup_required: false, authenticated: false, user: null }, { error: new Error("down") })).toBe("login");
    expect(view({ setup_required: true, authenticated: false, user: null })).toBe("setup");
    expect(view({ setup_required: false, authenticated: false, user: null })).toBe("login");
    expect(view(signedIn())).toBe("app");
  });

  it("flags a stale state only when cached data survives a failed refetch", () => {
    expect(authStateStale({ state: signedIn(), error: new Error("down") })).toBe(true);
    expect(authStateStale({ state: undefined, error: new Error("down") })).toBe(false);
    expect(authStateStale({ state: signedIn(), error: null })).toBe(false);
    expect(authStateStale({ state: signedIn({ must_change_password: true }), error: mustChange })).toBe(false);
  });

  it("shows the app again once the flag is cleared", () => {
    expect(view(signedIn({ must_change_password: true }))).toBe("change-password");
    expect(view(signedIn({ must_change_password: false }))).toBe("app");
  });
});

describe("api error listeners", () => {
  it("see HTTP errors, never swallow them, and can unsubscribe", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: { code: "must_change_password", message: "Change your password." } }), {
            status: 403,
          }),
      ),
    );
    const seen: ApiError[] = [];
    const off = onApiError((e) => {
      seen.push(e);
      throw new Error("listener bug"); // must not replace the request's error
    });
    const err = (await api.get("/instances/").catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(isMustChangePasswordError(err)).toBe(true);
    expect(seen).toEqual([err]);
    off();
    await api.get("/instances/").catch(() => undefined);
    expect(seen).toHaveLength(1);
  });
});
