/**
 * D-3 (security review 2026-11-11): failed sign-ins and wrong current passwords are limited per account;
 * the panel answers 429 `throttled` with `details.wait` (and Retry-After). The sign-in and password forms
 * say how long to wait, in words, instead of "Expected available in 540 seconds."
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, request, throttledMessage, throttleWait } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { AuthState } from "@/lib/types";
import { ForcedPasswordChange } from "./AccountPage";
import { LoginPage } from "./AuthPages";

const HINT =
  "Too many failed attempts for this account. Wait until the limit ends, sign in from a browser that has signed in to this account before, or run `manage.py unlock_login <username>` in the panel container.";
const locked = (wait = 540) => new ApiError(429, "throttled", `${HINT} Expected available in ${wait} seconds.`, { wait });

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("the wait of a 429 throttled", () => {
  it("comes from details.wait, else from Retry-After; other errors have none", async () => {
    expect(throttleWait(locked(540))).toBe(540);
    expect(throttleWait(new ApiError(400, "throttled", "?", { wait: 5 }))).toBeNull();
    expect(throttleWait(new Error("429"))).toBeNull();

    // the per-address limit: DRF's own throttle, Retry-After only
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ error: { code: "throttled", message: "Request was throttled. Expected available in 42 seconds.", details: {} } }), {
          status: 429,
          headers: { "Retry-After": "42" },
        }),
      ),
    );
    const err = await request("POST", "/auth/login", { body: {} }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).retryAfter).toBe(42);
    expect(throttleWait(err)).toBe(42);
  });

  it("is said in words with the time it ends, replacing the API's seconds", () => {
    const now = new Date(2026, 10, 11, 14, 23, 0).getTime();
    const at = new Date(now + 540_000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    expect(throttledMessage(locked(540), now)).toBe(`${HINT} Try again in 9 minutes (at ${at}).`);
    expect(throttledMessage(locked(1), now)).toMatch(/Try again in 1 second \(at /);
    expect(throttledMessage(locked(86_000), now)).toMatch(/Try again in 24 hours \(at /);
    expect(throttledMessage(new ApiError(429, "throttled", "Request was throttled."), now)).toBe("Request was throttled.");
    expect(throttledMessage(new ApiError(401, "authentication_failed", "Invalid credentials"), now)).toBeNull();
  });
});

function wrap(ui: React.ReactNode, state?: AuthState) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  if (state) qc.setQueryData(qk.auth, state);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("forms", () => {
  it("sign-in: the alert names the wait", async () => {
    vi.spyOn(api, "post").mockRejectedValue(locked(540));
    const user = wrap(<LoginPage />);
    await user.type(screen.getByLabelText("Username"), "owner");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Too many failed attempts for this account.");
    expect(alert).toHaveTextContent(/Try again in 9 minutes \(at .+\)\./);
    expect(alert).not.toHaveTextContent("Expected available");
  });

  it("sign-in: other errors are shown as before", async () => {
    vi.spyOn(api, "post").mockRejectedValue(new ApiError(400, "invalid_credentials", "Invalid username or password."));
    const user = wrap(<LoginPage />);
    await user.type(screen.getByLabelText("Username"), "owner");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/^Invalid username or password\.$/);
  });

  it("password change: the form names the wait", async () => {
    const state: AuthState = {
      setup_required: false,
      authenticated: true,
      user: {
        id: 3,
        username: "helper",
        email: "",
        first_name: "",
        last_name: "",
        is_owner: false,
        role_name: null,
        permissions: [],
        must_change_password: true,
        preferences: {},
      },
    };
    vi.spyOn(api, "get").mockResolvedValue(state as never);
    vi.spyOn(api, "post").mockRejectedValue(locked(120));
    const user = wrap(<ForcedPasswordChange />, state);
    await user.type(await screen.findByLabelText("Temporary password"), "temporary-one");
    await user.type(screen.getByLabelText("New password"), "a-much-better-secret");
    await user.type(screen.getByLabelText("Confirm new password"), "a-much-better-secret");
    await user.click(screen.getByRole("button", { name: "Set password and continue" }));
    expect(await screen.findByText(/Too many failed attempts for this account\. .* Try again in 2 minutes \(at .+\)\./)).toBeInTheDocument();
  });
});
