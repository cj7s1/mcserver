import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { authGateView } from "@/lib/auth-gate";
import type { AuthState, Me } from "@/lib/types";
import { STORAGE_OWNER_KEY } from "@/lib/user-storage";
import { useAuth, useAuthLifecycle } from "./use-auth";

const user = (over: Partial<Me> = {}): Me => ({
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
const signedIn = (over: Partial<Me> = {}): AuthState => ({ setup_required: false, authenticated: true, user: user(over) });
const mustChange = { error: { code: "must_change_password", message: "You must change your password first." } };

type Route = (method: string) => { status: number; body?: unknown };

/** fetch stub answering by path; `routes` may be swapped during a test. */
function stubApi(routes: Record<string, Route>) {
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const path = url.split("?")[0].replace(/^\/api/, "");
    const route = routes[path];
    const { status, body } = route ? route(init?.method ?? "GET") : { status: 404, body: { error: { code: "not_found", message: path } } };
    return new Response(body === undefined ? null : JSON.stringify(body), { status });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  const hook = renderHook(
    () => {
      const auth = useAuth();
      useAuthLifecycle(auth.state);
      return { ...auth, view: authGateView(auth) };
    },
    { wrapper },
  );
  return { qc, hook };
}

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
});

describe("useAuth / useAuthLifecycle", () => {
  it("switches to the password screen when any request is refused with must_change_password", async () => {
    let flagged = false;
    stubApi({
      "/auth/state": () => ({ status: 200, body: signedIn({ must_change_password: flagged }) }),
      "/instances/": () => ({ status: 403, body: mustChange }),
    });
    const { hook } = setup();
    await waitFor(() => expect(hook.result.current.view).toBe("app"));

    flagged = true; // an admin set a temporary password meanwhile; the cached state is stale
    await act(async () => {
      await api.get("/instances/").catch(() => undefined);
    });
    await waitFor(() => expect(hook.result.current.view).toBe("change-password"));
    expect(hook.result.current.user?.username).toBe("alice");
  });

  it("uses /auth/me when /auth/state itself is refused", async () => {
    stubApi({
      "/auth/state": () => ({ status: 403, body: mustChange }),
      "/auth/me": () => ({ status: 200, body: user({ must_change_password: true }) }),
    });
    const { hook } = setup();
    await waitFor(() => expect(hook.result.current.view).toBe("change-password"));
    expect(hook.result.current.error).toBeNull();
    expect(hook.result.current.user?.id).toBe(4);
  });

  it("shows the app again after the password was changed and auth refreshed", async () => {
    let flagged = true;
    stubApi({ "/auth/state": () => ({ status: 200, body: signedIn({ must_change_password: flagged }) }) });
    const { qc, hook } = setup();
    await waitFor(() => expect(hook.result.current.view).toBe("change-password"));
    flagged = false;
    await act(async () => {
      await qc.invalidateQueries();
    });
    await waitFor(() => expect(hook.result.current.view).toBe("app"));
  });

  it("wipes console history on logout", async () => {
    let authed = true;
    stubApi({
      "/auth/state": () => ({ status: 200, body: authed ? signedIn() : { setup_required: false, authenticated: false, user: null } }),
      "/auth/logout": () => {
        authed = false;
        return { status: 204 };
      },
    });
    const { hook } = setup();
    await waitFor(() => expect(hook.result.current.view).toBe("app"));
    await waitFor(() => expect(sessionStorage.getItem(STORAGE_OWNER_KEY)).toBe("4"));
    sessionStorage.setItem("mcpanel.console.history.u4.srv", JSON.stringify(["op alice"]));
    localStorage.setItem("mcpanel-theme", "light");

    await act(async () => {
      await hook.result.current.logout();
    });
    expect(sessionStorage.getItem("mcpanel.console.history.u4.srv")).toBeNull();
    expect(localStorage.getItem("mcpanel-theme")).toBe("light");
    await waitFor(() => expect(hook.result.current.view).toBe("login"));
  });

  it("drops the previous account's cached responses when another user signs in (e.g. from another tab)", async () => {
    let who = user();
    stubApi({ "/auth/state": () => ({ status: 200, body: { setup_required: false, authenticated: true, user: who } }) });
    const { qc, hook } = setup();
    await waitFor(() => expect(hook.result.current.user?.id).toBe(4));
    qc.setQueryData(["instances"], [{ id: "alices-server" }]);
    sessionStorage.setItem("mcpanel.console.history.u4.srv", JSON.stringify(["op alice"]));

    who = user({ id: 5, username: "bob" });
    await act(async () => {
      await qc.invalidateQueries({ queryKey: ["auth"] });
    });
    await waitFor(() => expect(hook.result.current.user?.id).toBe(5));
    expect(qc.getQueryData(["instances"])).toBeUndefined();
    expect(sessionStorage.getItem("mcpanel.console.history.u4.srv")).toBeNull();
    expect(sessionStorage.getItem(STORAGE_OWNER_KEY)).toBe("5");
  });

  it("wipes the previous user's data when a different user is signed in", async () => {
    sessionStorage.setItem(STORAGE_OWNER_KEY, "9");
    sessionStorage.setItem("mcpanel.console.history.u9.srv", JSON.stringify(["op mallory"]));
    stubApi({ "/auth/state": () => ({ status: 200, body: signedIn() }) });
    const { hook } = setup();
    await waitFor(() => expect(hook.result.current.view).toBe("app"));
    await waitFor(() => expect(sessionStorage.getItem(STORAGE_OWNER_KEY)).toBe("4"));
    expect(sessionStorage.getItem("mcpanel.console.history.u9.srv")).toBeNull();
  });
});
