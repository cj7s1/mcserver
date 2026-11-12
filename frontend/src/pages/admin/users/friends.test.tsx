/**
 * B4 Friends in the panel: Users → Invite a friend (POST /users/invite: the one-time link to copy), and the app of
 * a friend whose servers come back `limited: true`: a server page with status, Start/Stop and the Share card and
 * no tabs; the dashboard with their servers only (no stats, activity or host panels); no Modpacks or Activity
 * entries; nothing that would answer 403.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { AppLayout } from "@/components/layout/AppLayout";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ThemeProvider } from "@/hooks/use-theme";
import { api, ApiError } from "@/lib/api";
import { friendOnly, playersOnlineText } from "@/lib/limited";
import { qk } from "@/lib/queries";
import { instanceSections } from "@/lib/sections";
import type { AuthState, Instance, PermissionCode } from "@/lib/types";
import Dashboard from "@/pages/Dashboard";
import { InstanceLayout } from "@/pages/instance/InstanceLayout";
import Overview from "@/pages/instance/Overview";
import { ServerQuickActions } from "@/pages/library/ServerActions";
import { makeInstance } from "@/test/fixtures";
import { InviteFriendDialog } from "./InviteFriendDialog";

/** The short answer a friend gets for a server (only these keys; final shape: no connection). */
function limitedServer(over: Partial<Instance> = {}): Instance {
  const full = makeInstance({ id: "f1f1f1f1-0000-0000-0000-000000000001", name: "Vikings", game: "valheim", game_label: "Valheim", status: "stopped" });
  const keys = [
    "id", "name", "description", "icon_url", "group", "game", "game_label", "edition", "server_type", "minecraft_version", "loader",
    "is_modpack", "modpack_name", "modpack_version_name", "capabilities", "desired_state", "status", "status_detail", "health",
    "started_at", "ready_at", "status_updated_at", "active_job", "power_countdown", "archived_at",
  ] as const;
  const out = Object.fromEntries(keys.map((k) => [k, (full as unknown as Record<string, unknown>)[k]])) as unknown as Instance;
  return { ...out, permissions: ["instance.control", "instance.overview"], limited: true, players_online: null, players_max: null, ...over };
}

const authFor = (permissions: PermissionCode[], over: Partial<NonNullable<AuthState["user"]>> = {}): AuthState => ({
  setup_required: false,
  authenticated: true,
  user: { id: 9, username: "friend", email: "", first_name: "", last_name: "", is_owner: false, role_name: null, permissions, must_change_password: false, preferences: {}, ...over },
});

let gets: Record<string, unknown>;
const requested: string[] = [];
beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});
beforeEach(() => {
  gets = {};
  requested.length = 0;
  vi.spyOn(api, "get").mockImplementation(((path: string) => {
    requested.push(path);
    return path in gets ? Promise.resolve(structuredClone(gets[path])) : new Promise(() => {});
  }) as never);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("who is only a friend", () => {
  it("no global permission beyond the Friend role's and every server limited", () => {
    const user = authFor([]).user!;
    expect(friendOnly(user, [limitedServer()])).toBe(true);
    expect(friendOnly(authFor(["instance.control", "instance.overview"]).user, [limitedServer()])).toBe(true);
    expect(friendOnly(user, [limitedServer(), makeInstance()])).toBe(false);
    expect(friendOnly(authFor(["instance.view"]).user, [limitedServer()])).toBe(false);
    expect(friendOnly(authFor([], { is_owner: true }).user, [])).toBe(false);
    expect(friendOnly(user, undefined)).toBe(false);
  });

  it("says how many play, from the short answer", () => {
    expect(playersOnlineText({ players_online: 3, players_max: 10 })).toBe("3 of 10 players online");
    expect(playersOnlineText({ players_online: 1, players_max: null })).toBe("1 player online");
    expect(playersOnlineText({ players_online: 0, players_max: 10 })).toBe("0 of 10 players online");
    expect(playersOnlineText({ players_online: null, players_max: 10 })).toBeNull();
    expect(playersOnlineText({})).toBeNull();
  });

  it("a limited server has its overview only, and no Settings in its action menu", async () => {
    expect(instanceSections(limitedServer()).map((s) => s.key)).toEqual(["overview"]);
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ServerQuickActions instance={limitedServer()} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await userEvent.setup().click(screen.getByRole("button", { name: "More actions for Vikings" }));
    expect(await screen.findByRole("menuitem", { name: /Overview/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Settings/ })).not.toBeInTheDocument();
  });
});

describe("Users → Invite a friend", () => {
  function renderDialog() {
    gets["/instances/"] = [makeInstance({ id: "s1", name: "Lobby" }), makeInstance({ id: "s2", name: "Vikings" })];
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <InviteFriendDialog open onOpenChange={() => {}} />
      </QueryClientProvider>,
    );
    return userEvent.setup();
  }

  it("creates the friend and shows the one-time link to copy", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({
      user: { id: 12, username: "bob" },
      servers: [{ id: "s2", name: "Vikings" }],
      token: "tok",
      path: "/reset-password#token=abc123",
      expires_in_hours: 24,
    } as never);
    const user = renderDialog();
    await user.click(screen.getByRole("button", { name: /Invite/ }));
    expect(screen.getByText("Enter a user name")).toBeInTheDocument();
    expect(screen.getByText("Choose at least one server")).toBeInTheDocument();
    await user.type(screen.getByLabelText("User name"), "bob");
    await user.type(screen.getByLabelText("E-mail (optional)"), "bob@example.org");
    await user.click(await screen.findByRole("checkbox", { name: "Vikings" }));
    await user.click(screen.getByRole("button", { name: /Invite/ }));
    expect(post).toHaveBeenCalledWith("/users/invite", { username: "bob", email: "bob@example.org", servers: ["s2"] });
    expect(await screen.findByLabelText("Link for bob")).toHaveValue(`${window.location.origin}/reset-password#token=abc123`);
    expect(screen.getByText(/picks a password there\. It works once and for 24 hours/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Copy/ })).toBeInTheDocument();
  });

  it("shows the panel's refusal at the field", async () => {
    vi.spyOn(api, "post").mockRejectedValue(new ApiError(400, "validation_error", "Some fields are invalid.", { fields: { username: ["A user with that username already exists."] } }));
    const user = renderDialog();
    await user.type(screen.getByLabelText("User name"), "bob");
    await user.click(await screen.findByRole("checkbox", { name: "Lobby" }));
    await user.click(screen.getByRole("button", { name: /Invite/ }));
    expect(await screen.findByText("A user with that username already exists.")).toBeInTheDocument();
  });
});

describe("the app of a friend", () => {
  function renderAt(path: string, routes: Parameters<typeof createMemoryRouter>[0], instances: Instance[]) {
    gets["/instances/"] = instances;
    for (const i of instances) gets[`/instances/${i.id}`] = i;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    qc.setQueryData(qk.auth, authFor([]));
    for (const i of instances) qc.setQueryData(qk.instance(i.id), i);
    qc.setQueryData([...qk.instances, { archived: "false" }], instances);
    const router = createMemoryRouter(routes, { initialEntries: [path] });
    render(
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <TooltipProvider>
            <RouterProvider router={router} />
          </TooltipProvider>
        </ThemeProvider>
      </QueryClientProvider>,
    );
  }

  it("a server page: status, Start and the Share card; no tabs, no other page reached", async () => {
    const server = limitedServer({ status: "running", players_online: 2, players_max: 10 });
    gets[`/instances/${server.id}/share`] = {
      game: "valheim",
      game_label: "Valheim",
      address: { host: "play.example.org", source: "panel", note: "" },
      connect: "play.example.org:2456",
      ports: [],
      protection: { kind: "password", on: true, label: "a join password" },
      steps: ["Start Valheim."],
      requirements: [],
      open_warning: "",
      invite_text: "Join!",
      invite_has_password: false,
      checklist: [],
    };
    renderAt(`/servers/${server.id}/console`, [{ path: "/servers/:id", element: <InstanceLayout />, children: [{ index: true, element: <Overview /> }, { path: "console", element: <p>console page</p> }] }], [server]);
    expect(await screen.findByLabelText("Address friends type")).toHaveTextContent("play.example.org:2456");
    expect(screen.getByText(/2 of 10 players online/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Stop/ })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Server sections" })).not.toBeInTheDocument();
    expect(screen.queryByText("console page")).not.toBeInTheDocument();
    expect(screen.queryByText(/Java|port undefined/)).not.toBeInTheDocument();
    expect(requested.filter((p) => p !== `/instances/${server.id}` && p !== `/instances/${server.id}/share` && p !== "/auth/state")).toEqual([]);
  });

  it("the dashboard lists their servers without stats, activity or host panels", async () => {
    const running = limitedServer({ status: "running", players_online: 4, players_max: null });
    renderAt("/", [{ path: "/", element: <Dashboard /> }], [running]);
    expect(await screen.findByRole("link", { name: "Vikings" })).toBeInTheDocument();
    expect(screen.getByText("4 players online")).toBeInTheDocument();
    expect(screen.queryByText(/heap|:undefined/)).not.toBeInTheDocument();
    expect(screen.queryByText("Recent activity")).not.toBeInTheDocument();
    expect(screen.queryByText("Host capacity")).not.toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 20));
    expect(requested.some((p) => p.endsWith("/stats") || p === "/jobs")).toBe(false);
  });

  it("the navigation has no Modpacks or Activity", async () => {
    class NoSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      readyState = 0;
      onopen = null;
      onclose = null;
      onmessage = null;
      onerror = null;
      send() {}
      close() {}
    }
    vi.stubGlobal("WebSocket", NoSocket);
    renderAt("/", [{ element: <AppLayout />, children: [{ path: "/", element: <p>home</p> }] }], [limitedServer()]);
    const nav = (await screen.findAllByRole("navigation"))[0];
    await waitFor(() => expect(within(nav).queryByRole("link", { name: "Modpacks" })).not.toBeInTheDocument());
    expect(within(nav).getByRole("link", { name: "Dashboard" })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Activity" })).not.toBeInTheDocument();
  });
});

describe("a friend against the tightened API (review of 2026-11-12)", () => {
  const SHARE = {
    game: "valheim",
    game_label: "Valheim",
    address: { host: "play.example.org", source: "panel", note: "" },
    connect: "play.example.org:2456",
    ports: [],
    protection: { kind: "password", on: true, label: "a join password" },
    steps: ["Start Valheim."],
    requirements: [],
    open_warning: "",
    invite_text: "Join!",
    invite_has_password: false,
    checklist: [],
    password_set: true,
  };
  function renderAt(path: string, routes: Parameters<typeof createMemoryRouter>[0], instances: Instance[]) {
    gets["/instances/"] = instances;
    for (const i of instances) gets[`/instances/${i.id}`] = i;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    qc.setQueryData(qk.auth, authFor([]));
    for (const i of instances) qc.setQueryData(qk.instance(i.id), i);
    qc.setQueryData([...qk.instances, { archived: "false" }], instances);
    render(
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <TooltipProvider>
            <RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />
          </TooltipProvider>
        </ThemeProvider>
      </QueryClientProvider>,
    );
  }
  /** What a friend may call (everything else answers 403 for them or is not theirs). */
  const allowed = (id: string) => new Set(["/auth/state", "/auth/me", "/instances/", `/instances/${id}`, `/instances/${id}/share`]);

  class NoSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    readyState = 0;
    onopen = null;
    onclose = null;
    onmessage = null;
    onerror = null;
    send() {}
    close() {}
  }

  it("the whole app shell (dashboard, server list, server page, account) asks only for what a friend may read", async () => {
    vi.stubGlobal("WebSocket", NoSocket);
    const server = limitedServer({
      status: "running",
      players_online: 1,
      players_max: 10,
      active_job: { kind: "start", status: "running", progress: 0.5 } as unknown as Instance["active_job"],
    });
    gets["/instances/"] = [server];
    gets[`/instances/${server.id}`] = server;
    gets[`/instances/${server.id}/share`] = SHARE;
    gets["/auth/me"] = authFor([]).user;
    const [{ default: ServersPage }, { default: AccountPage }] = await Promise.all([import("@/pages/ServersPage"), import("@/pages/AccountPage")]);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(qk.auth, authFor([]));
    const router = createMemoryRouter(
      [
        {
          element: <AppLayout />,
          children: [
            { path: "/", element: <Dashboard /> },
            { path: "/servers", element: <ServersPage /> },
            { path: "/servers/:id", element: <InstanceLayout />, children: [{ index: true, element: <Overview /> }] },
            { path: "/account", element: <AccountPage /> },
          ],
        },
      ],
      { initialEntries: ["/"] },
    );
    render(
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <TooltipProvider>
            <RouterProvider router={router} />
          </TooltipProvider>
        </ThemeProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("1 of 10 players online")).toBeInTheDocument();
    expect(screen.getByText(/Starting · 50%/)).toBeInTheDocument();
    for (const path of ["/servers", `/servers/${server.id}`, "/account"]) {
      await act(async () => {
        await router.navigate(path);
      });
      await new Promise((r) => setTimeout(r, 30));
    }
    expect(await screen.findByText("Global permissions")).toBeInTheDocument();
    const outside = requested.filter((p) => !allowed(server.id).has(p));
    expect(outside).toEqual([]);
  });

  it("a running server: Stop only (plain, no warning), no Restart, Kill or Cancel; the job by its kind", async () => {
    const server = limitedServer({
      status: "running",
      active_job: null,
      power_countdown: { action: "restart", phase: "warning", action_at: new Date(Date.now() + 60_000).toISOString() } as unknown as Instance["power_countdown"],
    });
    gets[`/instances/${server.id}/share`] = SHARE;
    const post = vi.spyOn(api, "post").mockResolvedValue({ id: "j1", title: "Stop" } as never);
    renderAt(`/servers/${server.id}`, [{ path: "/servers/:id", element: <InstanceLayout />, children: [{ index: true, element: <Overview /> }] }], [server]);
    expect(await screen.findByText(/Restarting in/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cancel/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Restart/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Kill/ })).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^Stop$/ }));
    const dialog = await screen.findByRole("alertdialog", { name: "Stop Vikings?" });
    expect(within(dialog).queryByText("Warn players in game")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Stop server" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(`/instances/${server.id}/power`, { action: "stop" }));
    expect(requested.some((p) => p.endsWith("/stats"))).toBe(false);
  });

  it("the quick actions and the palette offer no restart", async () => {
    const { powerOptions } = await import("@/components/command-palette/entries");
    const running = limitedServer({ status: "running" });
    expect(powerOptions(running).map((o) => o.action)).toEqual(["stop"]);
    expect(powerOptions(limitedServer()).map((o) => o.action)).toEqual(["start"]);
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ServerQuickActions instance={running} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await userEvent.setup().click(screen.getByRole("button", { name: "More actions for Vikings" }));
    expect(await screen.findByRole("menuitem", { name: /Stop/ })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /Restart/ })).not.toBeInTheDocument();
  });

  it("global answers are asked for only when they are open to the user", async () => {
    const { globalAnswersAllowed } = await import("@/lib/limited");
    const friend = authFor([]).user!;
    expect(globalAnswersAllowed(friend, undefined)).toBe(false); // still loading: not asked
    expect(globalAnswersAllowed(friend, [limitedServer()])).toBe(false);
    expect(globalAnswersAllowed(friend, [])).toBe(false);
    expect(globalAnswersAllowed(friend, [makeInstance()])).toBe(true);
    expect(globalAnswersAllowed(authFor(["instance.create"]).user, undefined)).toBe(true);
    expect(friendOnly(friend, [])).toBe(true);
    expect(friendOnly(authFor(["instance.control", "instance.overview"]).user, [])).toBe(true);
  });
});
