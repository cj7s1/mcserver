import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LayoutDashboard, Plus, Server, Users } from "lucide-react";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "@/hooks/use-theme";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { AuthState, Instance, PermissionCode } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { CommandPalette, type PaletteNavGroup } from "./CommandPalette";
import { RECENT_KEY } from "./storage";

const NAV: PaletteNavGroup[] = [
  {
    heading: "Navigation",
    items: [
      { to: "/", label: "Dashboard", icon: <LayoutDashboard /> },
      { to: "/servers", label: "Servers", icon: <Server /> },
      { to: "/servers/new", label: "Create server", icon: <Plus />, perm: "instance.create" },
    ],
  },
  { heading: "Administration", items: [{ to: "/admin/users", label: "Users & roles", icon: <Users />, perm: "system.users" }] },
];

const RUNNING = makeInstance({ id: "aaaaaaaa-0000-0000-0000-000000000001", name: "Vanilla Test", status: "running" });
const VIEW_ONLY = makeInstance({
  id: "aaaaaaaa-0000-0000-0000-000000000002",
  name: "Paper Lobby",
  status: "stopped",
  server_type: "PAPER",
  permissions: ["instance.view"],
});
const STOPPED = makeInstance({ id: "aaaaaaaa-0000-0000-0000-000000000003", name: "Fabric Test", status: "stopped", server_type: "FABRIC" });

function authState(perms: PermissionCode[]): AuthState {
  return {
    setup_required: false,
    authenticated: true,
    user: {
      id: 1,
      username: "tester",
      email: "",
      first_name: "",
      last_name: "",
      is_owner: false,
      role_name: "Operator",
      permissions: perms,
      must_change_password: false,
      preferences: {},
    },
  };
}

function Where() {
  const loc = useLocation();
  return <output data-testid="where">{loc.pathname}</output>;
}

function setup({ path = "/", perms = ["instance.create"] as PermissionCode[], instances = [RUNNING, VIEW_ONLY, STOPPED] as Instance[] } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false }, mutations: { retry: false } } });
  qc.setQueryData(qk.auth, authState(perms));
  qc.setQueryData([...qk.instances, { archived: "false" }], instances);
  qc.setQueryData([...qk.instances, { archived: "true" }], []);
  for (const i of instances) qc.setQueryData(qk.instance(i.id), i);
  const user = userEvent.setup();
  render(
    <ThemeProvider>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <CommandPalette navigation={NAV} />
          <input aria-label="Some field" />
          <button type="button">Plain button</button>
          <Where />
        </MemoryRouter>
      </QueryClientProvider>
    </ThemeProvider>,
  );
  return { user };
}

let post: ReturnType<typeof vi.spyOn>;

beforeAll(() => {
  // jsdom lacks these; cmdk measures the list and scrolls the selected item into view.
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Element.prototype.scrollIntoView ??= function scrollIntoView() {};
});

beforeEach(() => {
  localStorage.clear();
  vi.spyOn(api, "get").mockImplementation((async (path: string) => {
    throw new Error(`unexpected GET ${path}`);
  }) as typeof api.get);
  post = vi.spyOn(api, "post").mockResolvedValue({ id: "job-1" } as never);
});

afterEach(() => {
  vi.restoreAllMocks();
});

const where = () => screen.getByTestId("where").textContent;

describe("CommandPalette", () => {
  it("opens with Ctrl+K and the search button, closes with Escape and Ctrl+K", async () => {
    const { user } = setup();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    expect(within(dialog).getByRole("combobox")).toHaveFocus();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Search pages, servers and actions" }));
    await screen.findByRole("dialog", { name: "Command palette" });
    await user.keyboard("{Meta>}k{/Meta}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("hands focus back to what had it when the palette opened (Radix has no trigger to return to)", async () => {
    const { user } = setup();
    const field = screen.getByRole("textbox", { name: "Some field" });
    await user.click(field);
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("dialog", { name: "Command palette" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(field).toHaveFocus());

    // opened from the search button, focus returns to it (also after a follow-up dialog)
    const trigger = screen.getByRole("button", { name: "Search pages, servers and actions" });
    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    await user.type(within(dialog).getByRole("combobox"), "keyboard shortcuts");
    await user.keyboard("{Enter}");
    const help = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    await waitFor(() => expect(help).toContainElement(document.activeElement as HTMLElement));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());

    // "?" from a focused button returns focus to that button
    const plain = screen.getByRole("button", { name: "Plain button" });
    await user.click(plain);
    await user.keyboard("?");
    await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(plain).toHaveFocus());
  });

  it("lists permitted navigation and every visible server, and navigates on select", async () => {
    const { user } = setup();
    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    expect(within(dialog).getByRole("option", { name: /Dashboard/ })).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: /Create server/ })).toBeInTheDocument();
    expect(within(dialog).queryByRole("option", { name: /Users & roles/ })).not.toBeInTheDocument();
    for (const name of ["Vanilla Test", "Paper Lobby", "Fabric Test"]) {
      expect(within(dialog).getByRole("option", { name: new RegExp(name) })).toBeInTheDocument();
    }
    await user.type(within(dialog).getByRole("combobox"), "lobby");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(where()).toBe(`/servers/${VIEW_ONLY.id}`));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]")).toEqual([`server:${VIEW_ONLY.id}:overview`]);

    // reopening shows it under "Recent"
    await user.keyboard("{Control>}k{/Control}");
    const again = await screen.findByRole("dialog", { name: "Command palette" });
    const recent = within(again).getByRole("group", { name: "Recent" });
    expect(within(recent).getByRole("option", { name: /Paper Lobby/ })).toBeInTheDocument();
  });

  it("finds server pages with a fuzzy, order-independent query", async () => {
    const { user } = setup();
    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    await user.type(within(dialog).getByRole("combobox"), "cons vanil");
    const option = within(dialog).getByRole("option", { name: /Vanilla Test.*Console/ });
    await user.click(option);
    await waitFor(() => expect(where()).toBe(`/servers/${RUNNING.id}/console`));
  });

  it("orders groups by their best match, so the top result is selected", async () => {
    const { user } = setup();
    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    await user.type(within(dialog).getByRole("combobox"), "console");
    await waitFor(() => {
      const selected = dialog.querySelector('[cmdk-item][aria-selected="true"]');
      expect(selected?.textContent).toMatch(/Console/);
    });
    const headings = [...dialog.querySelectorAll("[cmdk-group-heading]")].map((h) => h.textContent);
    expect(headings[0]).toBe("Server pages");
  });

  it("asks before stopping a server and sends nothing when cancelled", async () => {
    const { user } = setup();
    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    await user.type(within(dialog).getByRole("combobox"), "stop vanilla");
    await user.click(within(dialog).getByRole("option", { name: /Stop Vanilla Test/ }));

    const confirm = await screen.findByRole("alertdialog", { name: "Stop Vanilla Test?" });
    expect(screen.queryByRole("dialog", { name: "Command palette" })).not.toBeInTheDocument();
    await waitFor(() => expect(within(confirm).getByRole("button", { name: "Cancel" })).toHaveFocus());
    await user.click(within(confirm).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(post).not.toHaveBeenCalled();
    // focus is not dropped on <body>: the palette was opened from the page, so it goes to the search button
    await waitFor(() => expect(screen.getByRole("button", { name: "Search pages, servers and actions" })).toHaveFocus());

    // confirming sends the same power request as the library buttons
    await user.keyboard("{Control>}k{/Control}");
    const again = await screen.findByRole("dialog", { name: "Command palette" });
    await user.type(within(again).getByRole("combobox"), "stop vanilla");
    await user.click(within(again).getByRole("option", { name: /Stop Vanilla Test/ }));
    const confirm2 = await screen.findByRole("alertdialog", { name: "Stop Vanilla Test?" });
    await user.click(within(confirm2).getByRole("button", { name: "Stop server" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(`/instances/${RUNNING.id}/power`, { action: "stop" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("starts a stopped server without confirmation and hides actions without instance.control", async () => {
    const { user } = setup();
    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    await user.type(within(dialog).getByRole("combobox"), "start");
    expect(within(dialog).queryByRole("option", { name: /Start Paper Lobby/ })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("option", { name: /Start Fabric Test/ }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(`/instances/${STOPPED.id}/power`, { action: "start" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("offers the current server's actions and pages before typing", async () => {
    const { user } = setup({ path: `/servers/${RUNNING.id}/files` });
    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    const group = within(dialog).getByRole("group", { name: "Current server · Vanilla Test" });
    expect(within(group).getByRole("option", { name: /Stop server/ })).toBeInTheDocument();
    expect(within(group).getByRole("option", { name: /Restart server/ })).toBeInTheDocument();
    expect(within(group).getByRole("option", { name: /Console/ })).toBeInTheDocument();
    await user.click(within(group).getByRole("option", { name: /Restart server/ }));
    const confirm = await screen.findByRole("alertdialog", { name: "Restart Vanilla Test?" });
    expect(post).not.toHaveBeenCalled();
    await user.click(within(confirm).getByRole("button", { name: "Restart" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(`/instances/${RUNNING.id}/power`, { action: "restart" }));
  });

  it("uses the regular Stop/Restart dialog, with the remembered in-game warning", async () => {
    // chosen once in any Stop/Restart dialog (per-browser preference)
    localStorage.setItem("mcpanel.power.warnMinutes", "10");
    const { user } = setup({ path: `/servers/${RUNNING.id}` });
    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    await user.click(within(within(dialog).getByRole("group", { name: "Current server · Vanilla Test" })).getByRole("option", { name: /Restart server/ }));
    const confirm = await screen.findByRole("alertdialog", { name: "Restart Vanilla Test?" });
    expect(within(confirm).getByText(/Players are disconnected while the server restarts/)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Restart in 10 min" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(`/instances/${RUNNING.id}/power`, { action: "restart", warn_minutes: 10 }),
    );
  });
});

describe("global shortcuts", () => {
  it("navigates with g d / g s / c and opens help with ?", async () => {
    const { user } = setup({ path: "/activity" });
    await user.keyboard("gs");
    expect(where()).toBe("/servers");
    await user.keyboard("gd");
    expect(where()).toBe("/");
    await user.keyboard("c");
    expect(where()).toBe("/servers/new");
    await user.keyboard("?");
    const help = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(help).getByText("Create a server")).toBeInTheDocument();
    // single keys do nothing while a dialog is open
    await user.keyboard("gs");
    expect(where()).toBe("/servers/new");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("ignores single-key shortcuts while typing and without permission", async () => {
    const { user } = setup({ path: "/activity", perms: [] });
    await user.click(screen.getByRole("textbox", { name: "Some field" }));
    await user.keyboard("gs");
    expect(where()).toBe("/activity");
    expect(screen.getByRole("textbox", { name: "Some field" })).toHaveValue("gs");
    await user.click(screen.getByRole("button", { name: "Plain button" }));
    await user.keyboard("c");
    expect(where()).toBe("/activity"); // no instance.create
    await user.keyboard("gs");
    expect(where()).toBe("/servers");
  });

  it("can switch single-key shortcuts off (Ctrl+K keeps working)", async () => {
    const { user } = setup({ path: "/activity" });
    await user.keyboard("?");
    const help = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    await user.click(within(help).getByRole("switch", { name: "Single-key shortcuts" }));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.keyboard("gs");
    expect(where()).toBe("/activity");
    await user.keyboard("{Control>}k{/Control}");
    expect(await screen.findByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
  });
});
