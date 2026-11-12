/**
 * B4 Friends in the panel: Users → Invite a friend (POST /users/invite: the one-time link to copy), and the app of
 * a friend whose servers come back `limited: true`: a server page with status, Start/Stop and the Share card and
 * no tabs; the dashboard with their servers only (no stats, activity or host panels); no Modpacks or Activity
 * entries; nothing that would answer 403.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
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

/** The short answer a friend gets for a server (only these keys). */
function limitedServer(over: Partial<Instance> = {}): Instance {
  const full = makeInstance({ id: "f1f1f1f1-0000-0000-0000-000000000001", name: "Vikings", game: "valheim", game_label: "Valheim", status: "stopped" });
  const keys = [
    "id", "name", "description", "icon_url", "group", "game", "game_label", "edition", "server_type", "minecraft_version", "loader",
    "is_modpack", "modpack_name", "modpack_version_name", "capabilities", "desired_state", "status", "status_detail", "health",
    "started_at", "ready_at", "status_updated_at", "active_job", "power_countdown", "archived_at", "connection",
  ] as const;
  const out = Object.fromEntries(keys.map((k) => [k, (full as unknown as Record<string, unknown>)[k]])) as unknown as Instance;
  return { ...out, permissions: ["instance.control"], limited: true, ...over };
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
