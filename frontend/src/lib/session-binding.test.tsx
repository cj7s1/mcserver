/**
 * D-1 / I-7 (security review 2026-11-11): the session cookie alone is no sign-in. The token of the login
 * (or setup) answer is stored, sent in `X-MCPanel-Session-Token` with every `/api/` request (fetch and the
 * XHR uploads), as the first frame of every WebSocket, and never in a URL; link downloads get a ticket; a
 * refused token signs the tab out with "please sign in again" and without a refetch loop.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DownloadLink } from "@/components/DownloadLink";
import { useAuth, useAuthLifecycle } from "@/hooks/use-auth";
import { LoginPage, SetupPage } from "@/pages/AuthPages";
import { useConsoleSocket } from "@/pages/instance/console/use-console-socket";
import { api, ApiError, ensureCsrfCookie, request, uploadWithProgress } from "./api";
import { authGateView } from "./auth-gate";
import { startDownload, ticketUrl } from "./downloads";
import { useLiveEvents } from "./events";
import { qk, useAuthState } from "./queries";
import { getSessionToken, SESSION_TOKEN_HEADER, SESSION_TOKEN_KEY, setSessionToken } from "./session-token";
import type { AuthState, Me } from "./types";
import { syncStorageOwner } from "./user-storage";

const TOKEN = "tok-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const ME: Me = {
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
};
const SIGNED_IN: AuthState = { setup_required: false, authenticated: true, user: ME };
const NEEDS_TOKEN: AuthState = { setup_required: false, authenticated: false, user: null, session_token_required: true };
const REFUSED = { error: { code: "session_token_required", message: "Sign in again.", details: {} } };

type Call = { url: string; init: RequestInit };

/** fetch stub answering by path; records every call. */
function stubFetch(routes: Record<string, (init: RequestInit) => { status: number; body?: unknown }>) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      const path = url.split("?")[0].replace(/^\/api/, "");
      const route = routes[path];
      const { status, body } = route ? route(init) : { status: 404, body: { error: { code: "not_found", message: path } } };
      return new Response(body === undefined ? null : JSON.stringify(body), { status });
    }),
  );
  return calls;
}

const headerOf = (c: Call | undefined) => (c?.init.headers as Record<string, string> | undefined)?.[SESSION_TOKEN_HEADER];

beforeEach(() => {
  document.cookie = "mcpanel_csrftoken=csrf1";
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  localStorage.clear();
  sessionStorage.clear();
});

describe("the session token on requests", () => {
  it("goes with every request (safe methods too) once stored, never in the URL", async () => {
    const calls = stubFetch({ "/instances/": () => ({ status: 200, body: [] }), "/auth/logout": () => ({ status: 204 }) });
    await request("GET", "/instances/");
    expect(headerOf(calls[0])).toBeUndefined();

    setSessionToken(TOKEN);
    expect(localStorage.getItem(SESSION_TOKEN_KEY)).toBe(TOKEN);
    await request("GET", "/instances/", { query: { archived: "false" } });
    await request("POST", "/auth/logout");
    expect(headerOf(calls[1])).toBe(TOKEN);
    expect(headerOf(calls[2])).toBe(TOKEN);
    expect((calls[2].init.headers as Record<string, string>)["X-CSRFToken"]).toBe("csrf1");
    for (const c of calls) expect(c.url).not.toContain(TOKEN);
  });

  it("goes with the CSRF primer and with XHR uploads", async () => {
    setSessionToken(TOKEN);
    document.cookie = "mcpanel_csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
    const calls = stubFetch({ "/auth/state": () => ({ status: 200, body: NEEDS_TOKEN }) });
    await ensureCsrfCookie();
    expect(headerOf(calls[0])).toBe(TOKEN);

    const sent: Record<string, string> = {};
    class FakeXhr {
      status = 200;
      responseText = "{}";
      upload: { onprogress: unknown } = { onprogress: null };
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      withCredentials = false;
      url = "";
      open(_m: string, url: string) {
        this.url = url;
      }
      setRequestHeader(k: string, v: string) {
        sent[k] = v;
      }
      send() {
        this.onload?.();
      }
    }
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
    await uploadWithProgress("/instances/x/files/upload", new FormData(), { path: "" });
    expect(sent[SESSION_TOKEN_HEADER]).toBe(TOKEN);
  });

  it("a 401 session_token_required drops the token it was sent with, but not a newer one", async () => {
    setSessionToken(TOKEN);
    let swap = false;
    stubFetch({
      "/instances/": () => {
        if (swap) setSessionToken("newer-token-from-another-tab");
        return { status: 401, body: REFUSED };
      },
    });
    const err = await request("GET", "/instances/").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 401, code: "session_token_required" });
    expect(getSessionToken()).toBeNull();

    setSessionToken(TOKEN);
    swap = true;
    await request("GET", "/instances/").catch(() => undefined);
    expect(getSessionToken()).toBe("newer-token-from-another-tab");
  });

  it("survives the storage clean-up of a sign-in (the stored owner changes) but not logout", async () => {
    setSessionToken(TOKEN);
    localStorage.setItem("mcpanel.servers.collapsedGroups", "[]");
    syncStorageOwner(4);
    expect(localStorage.getItem("mcpanel.servers.collapsedGroups")).toBeNull();
    expect(getSessionToken()).toBe(TOKEN);
    syncStorageOwner(null);
    expect(getSessionToken()).toBe(TOKEN); // the login page itself does not drop it; a sign-in replaces it

    let authed = true;
    const calls = stubFetch({
      "/auth/state": () => ({ status: 200, body: authed ? SIGNED_IN : { setup_required: false, authenticated: false, user: null } }),
      "/auth/logout": () => {
        authed = false;
        return { status: 204 };
      },
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useAuth(), { wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider> });
    await waitFor(() => expect(result.current.user?.id).toBe(4));
    await act(async () => {
      await result.current.logout();
    });
    // R-13: the logout itself carries the token (the panel ends a token session only then); afterwards it is gone
    expect(headerOf(calls.find((c) => c.url === "/api/auth/logout"))).toBe(TOKEN);
    expect(getSessionToken()).toBeNull();
  });

  it("a failed logout still drops the token in this browser", async () => {
    setSessionToken(TOKEN);
    stubFetch({
      "/auth/state": () => ({ status: 200, body: SIGNED_IN }),
      "/auth/logout": () => ({ status: 503, body: { error: { code: "service_unavailable", message: "down" } } }),
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useAuth(), { wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider> });
    await waitFor(() => expect(result.current.user?.id).toBe(4));
    await act(async () => {
      await result.current.logout().catch(() => undefined);
    });
    expect(getSessionToken()).toBeNull();
  });
});

describe("signing in", () => {
  function renderPage(ui: ReactNode, state: AuthState) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    qc.setQueryData(qk.auth, state);
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>{ui}</MemoryRouter>
      </QueryClientProvider>,
    );
    return userEvent.setup();
  }

  it("the login answer's token is stored before the state is read again; the note asks to sign in again", async () => {
    const calls = stubFetch({
      "/auth/login": () => ({ status: 200, body: { ...ME, session_token: TOKEN } }),
      "/auth/state": () => ({ status: 200, body: SIGNED_IN }),
    });
    function StateObserver() {
      useAuthState(); // the auth gate's observer: the login's invalidation reads the state again
      return null;
    }
    const user = renderPage(
      <>
        <StateObserver />
        <LoginPage />
      </>,
      NEEDS_TOKEN,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Your session needs a new sign-in. Please sign in again.");
    await user.type(screen.getByLabelText("Username"), "alice");
    await user.type(screen.getByLabelText("Password"), "correct horse");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(getSessionToken()).toBe(TOKEN));
    const state = await waitFor(() => {
      const c = calls.find((x) => x.url.startsWith("/api/auth/state"));
      expect(c).toBeDefined();
      return c;
    });
    expect(headerOf(state)).toBe(TOKEN);
  });

  it("no note after an ordinary sign-out", () => {
    renderPage(<LoginPage />, { setup_required: false, authenticated: false, user: null });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("the setup answer's token is stored too", async () => {
    stubFetch({
      "/auth/setup": () => ({ status: 201, body: { ...ME, is_owner: true, session_token: TOKEN } }),
      "/auth/state": () => ({ status: 200, body: SIGNED_IN }),
    });
    const user = renderPage(<SetupPage />, { setup_required: true, authenticated: false, user: null });
    await user.type(screen.getByLabelText("Username"), "owner");
    await user.type(screen.getByLabelText("Password"), "a-long-password-1");
    await user.type(screen.getByLabelText("Confirm password"), "a-long-password-1");
    await user.click(screen.getByRole("button", { name: /Create owner account/ }));
    await waitFor(() => expect(getSessionToken()).toBe(TOKEN));
  });
});

describe("a refused token signs the tab out", () => {
  function gate() {
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

  it("a 401 from any request shows the login page with the note; more refusals change nothing", async () => {
    setSessionToken(TOKEN);
    let stateCalls = 0;
    stubFetch({
      "/auth/state": (init) => {
        stateCalls += 1;
        const sent = (init.headers as Record<string, string>)[SESSION_TOKEN_HEADER];
        return { status: 200, body: sent === TOKEN ? SIGNED_IN : NEEDS_TOKEN };
      },
      "/instances/": () => ({ status: 401, body: REFUSED }),
    });
    const { qc, hook } = gate();
    await waitFor(() => expect(hook.result.current.view).toBe("app"));
    await act(async () => {
      await api.get("/instances/").catch(() => undefined);
    });
    await waitFor(() => expect(hook.result.current.view).toBe("login"));
    expect(qc.getQueryData<AuthState>(qk.auth)?.session_token_required).toBe(true);
    expect(getSessionToken()).toBeNull();
    const after = stateCalls;
    await act(async () => {
      await Promise.all([api.get("/instances/").catch(() => undefined), api.get("/instances/").catch(() => undefined)]);
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(stateCalls).toBe(after);
    expect(hook.result.current.view).toBe("login");
  });

  it("auth/state saying session_token_required drops the stored token and shows the login page", async () => {
    setSessionToken("stale-token");
    stubFetch({ "/auth/state": () => ({ status: 200, body: NEEDS_TOKEN }) });
    const { hook } = gate();
    await waitFor(() => expect(hook.result.current.view).toBe("login"));
    expect(getSessionToken()).toBeNull();
  });
});

class FakeSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static all: FakeSocket[] = [];
  readyState = FakeSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeSocket.all.push(this);
  }
  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = FakeSocket.CLOSED;
  }
}

describe("WebSockets", () => {
  beforeEach(() => {
    FakeSocket.all = [];
    vi.stubGlobal("WebSocket", FakeSocket);
    setSessionToken(TOKEN);
  });

  function wrap() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(qk.auth, SIGNED_IN);
    return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }

  const serverSends = (sock: FakeSocket, msg: unknown) => act(() => sock.onmessage?.({ data: JSON.stringify(msg) }));

  it("the live events socket sends the auth frame first and is live on auth_ok; the token is not in its URL", () => {
    const view = renderHook(() => useLiveEvents(true), { wrapper: wrap() });
    const sock = FakeSocket.all[0];
    expect(sock.sent).toEqual([]);
    act(() => sock.open());
    expect(sock.sent).toEqual([JSON.stringify({ type: "auth", token: TOKEN })]);
    expect(view.result.current).toBe(false); // open, but not accepted yet
    serverSends(sock, { type: "auth_ok" });
    expect(view.result.current).toBe(true);
    expect(sock.url).not.toContain(TOKEN);
    view.unmount();
  });

  it("a panel without auth_ok: the first event makes the events socket live", () => {
    const view = renderHook(() => useLiveEvents(true), { wrapper: wrap() });
    const sock = FakeSocket.all[0];
    act(() => sock.open());
    serverSends(sock, { type: "instance.status", instance_id: "x", payload: {} });
    expect(view.result.current).toBe(true);
    view.unmount();
  });

  it("the console: auth frame first; a command sent before auth_ok waits for it and is not shown to the page", () => {
    const seen: string[] = [];
    const view = renderHook(() => useConsoleSocket("abc", (m) => seen.push(m.type)), { wrapper: wrap() });
    const sock = FakeSocket.all[0];
    expect(view.result.current.send({ type: "command", id: "c0", command: "list" })).toBe(false);
    act(() => sock.open());
    expect(view.result.current.send({ type: "command", id: "c1", command: "list" })).toBe(true);
    expect(sock.sent.map((f) => JSON.parse(f).type)).toEqual(["auth"]);
    serverSends(sock, { type: "auth_ok" });
    expect(sock.sent.map((f) => JSON.parse(f).type)).toEqual(["auth", "command"]);
    expect(JSON.parse(sock.sent[1])).toEqual({ type: "command", id: "c1", command: "list" });
    expect(seen).not.toContain("auth_ok");
    serverSends(sock, { type: "hello", can_command: true });
    expect(view.result.current.send({ type: "command", id: "c2", command: "say hi" })).toBe(true);
    expect(sock.sent).toHaveLength(3);
    expect(JSON.parse(sock.sent[0])).toEqual({ type: "auth", token: TOKEN });
    expect(sock.url).toBe(`ws://${window.location.host}/ws/instances/abc/console/`);
    view.unmount();
  });

  it("the console with a panel that sends no auth_ok: its hello accepts the socket", () => {
    const view = renderHook(() => useConsoleSocket("abc", () => {}), { wrapper: wrap() });
    const sock = FakeSocket.all[0];
    act(() => sock.open());
    view.result.current.send({ type: "command", id: "c1", command: "list" });
    serverSends(sock, { type: "hello", can_command: true });
    expect(sock.sent.map((f) => JSON.parse(f).type)).toEqual(["auth", "command"]);
    expect(view.result.current.state).toBe("connected");
    view.unmount();
  });
});

describe("link downloads get a ticket", () => {
  const URL_PLAIN = "/api/instances/i1/files/download?path=world%2Flevel.dat";
  const URL_TICKET = `${URL_PLAIN}&ticket=abc.def`;

  it("asks for a ticket for exactly the link's URL and follows the answered URL", async () => {
    setSessionToken(TOKEN);
    const post = vi.spyOn(api, "post").mockResolvedValue({ url: URL_TICKET, expires_in: 60 } as never);
    const clicked: { href: string; download: string }[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ href: this.getAttribute("href") ?? "", download: this.download });
    });
    await startDownload(URL_PLAIN, "level.dat");
    expect(post).toHaveBeenCalledWith("/auth/download-ticket", { url: URL_PLAIN });
    expect(JSON.stringify(post.mock.calls)).not.toContain(TOKEN);
    expect(clicked).toEqual([{ href: URL_TICKET, download: "level.dat" }]);
    expect(document.querySelectorAll("a").length).toBe(0); // the helper link is gone again
  });

  it("never follows an answer that is no panel path; a refusal reaches the caller", async () => {
    vi.spyOn(api, "post").mockResolvedValueOnce({ url: "https://elsewhere.example/x" } as never);
    await expect(ticketUrl(URL_PLAIN)).rejects.toThrow(/did not issue a download ticket/);
    vi.spyOn(api, "post").mockRejectedValueOnce(new ApiError(400, "invalid_url", "Only the panel's download links can get a ticket"));
    await expect(ticketUrl("/api/instances/")).rejects.toMatchObject({ code: "invalid_url" });
  });

  it("DownloadLink keeps the plain link and downloads through a ticket on click and middle click", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ url: URL_TICKET } as never);
    const clicked: string[] = [];
    const realClick = HTMLAnchorElement.prototype.click;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      if (this.getAttribute("href") === URL_TICKET) clicked.push(this.getAttribute("href")!);
      else realClick.call(this);
    });
    render(
      <DownloadLink href={URL_PLAIN} download="level.dat">
        Download
      </DownloadLink>,
    );
    const link = screen.getByRole("link", { name: "Download" });
    expect(link).toHaveAttribute("href", URL_PLAIN);
    const user = userEvent.setup();
    await user.click(link);
    await waitFor(() => expect(clicked).toEqual([URL_TICKET]));
    await user.pointer({ keys: "[MouseMiddle]", target: link });
    await waitFor(() => expect(clicked).toHaveLength(2));
    expect(post).toHaveBeenCalledTimes(2);
    expect(post).toHaveBeenCalledWith("/auth/download-ticket", { url: URL_PLAIN });
  });
});
