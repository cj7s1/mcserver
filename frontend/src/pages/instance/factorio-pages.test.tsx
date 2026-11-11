/**
 * The instance pages of a non-Minecraft server (a Factorio fixture): which sections it shows, and the
 * flows the generic game pieces add (game settings, named ports, setup card, players, saves, console).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { serverSectionEntries } from "@/components/command-palette/entries";
import { TooltipProvider } from "@/components/ui/tooltip";
import * as apiModule from "@/lib/api";
import { api } from "@/lib/api";
import { MASK } from "@/lib/game-config";
import { consoleRules } from "@/lib/games";
import { qk } from "@/lib/queries";
import type { Instance, PermissionCode } from "@/lib/types";
import { factorioGame, makeFactorioInstance } from "@/test/fixtures";
import { SavesImportDialog, SavesResetDialog } from "./backups/SavesDialogs";
import { CommandInput } from "./console/CommandInput";
import { InstanceLayout } from "./InstanceLayout";
import { GameNetwork } from "./network/GameNetwork";
import { validateNamedPorts } from "./network/named-ports";
import Overview from "./Overview";
import { SetupCard } from "./overview/SetupCard";
import { GamePlayers } from "./players/GamePlayers";
import { gamePlayerNameError, listLabels } from "./players/game-players-model";
import Settings from "./Settings";
import { GameSettingsTab } from "./settings/game-settings-tab";

const ALL_PERMS: PermissionCode[] = [
  "instance.view",
  "instance.control",
  "instance.console",
  "instance.command",
  "instance.configure",
  "instance.files.read",
  "instance.files.write",
  "instance.content",
  "instance.players",
  "instance.backup",
  "instance.restore",
  "instance.schedules",
  "instance.access",
  "instance.delete",
];

let gets: Record<string, unknown> = {};

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Element.prototype.scrollIntoView ??= function scrollIntoView() {};
});

beforeEach(() => {
  gets = { "/catalog/games": [factorioGame()] };
  vi.spyOn(api, "get").mockImplementation(((path: string) =>
    path in gets ? Promise.resolve(structuredClone(gets[path])) : new Promise(() => {})) as never);
});
afterEach(() => vi.restoreAllMocks());

function wrap(children: ReactNode, qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  qc.setQueryData(qk.auth, { setup_required: false, authenticated: true, user: null });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter>{children}</MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

const factorio = (over: Partial<Instance> = {}) => makeFactorioInstance({ permissions: ALL_PERMS, ...over });

describe("sections of a Factorio server", () => {
  function renderAt(instance: Instance, sub: "" | "settings") {
    gets[`/instances/${instance.id}`] = instance;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    qc.setQueryData(qk.instance(instance.id), instance);
    const router = createMemoryRouter(
      [
        {
          path: "/servers/:id",
          element: <InstanceLayout />,
          children: [
            { index: true, element: <Overview /> },
            { path: "settings", element: <Settings /> },
          ],
        },
      ],
      { initialEntries: [`/servers/${instance.id}${sub ? `/${sub}` : ""}`] },
    );
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
  }

  it("shows no Minecraft-only section and names the game in the header", () => {
    const instance = factorio();
    renderAt(instance, "");
    const nav = screen.getByRole("navigation", { name: "Server sections" });
    const tabs = within(nav).getAllByRole("link").map((a) => a.textContent?.trim());
    expect(tabs).toEqual(["Overview", "Console", "Files", "Players", "Backups", "Schedules", "Network", "Settings", "Access", "Activity"]);
    expect(serverSectionEntries(instance).map((e) => e.label)).toEqual(tabs);
    expect(screen.getByText("Factorio 2.1.24 · port 34197/udp")).toBeInTheDocument();
  });

  it("opens Settings on Game settings; no Properties tab, no JVM wording", () => {
    renderAt(factorio(), "settings");
    const list = screen.getByRole("tablist", { name: "Settings sections" });
    expect(within(list).getAllByRole("tab").map((t) => t.textContent?.trim())).toEqual([
      "Game settings",
      "Resources",
      "Software",
      "Environment",
      "General",
      "Danger zone",
    ]);
    expect(within(list).getByRole("tab", { name: "Game settings" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("Game settings tab", () => {
  const instance = factorio();
  beforeEach(() => {
    gets[`/instances/${instance.id}/config`] = {
      files: [
        {
          id: "server-settings",
          path: "config/server-settings.json",
          label: "Server settings",
          codec: "json",
          exists: true,
          values: { name: "Factory", game_password: MASK, max_players: 0, "visibility.public": false, port: 34197 },
          fields: [
            ...factorioGame().config_files![0].fields,
            { key: "port", type: "int", label: "Port" },
          ],
          managed_keys: ["port"],
          redacted_keys: ["game_password"],
        },
      ],
    };
  });

  it("sends only what changed, never the masked secret, and hides managed keys", async () => {
    const patch = vi.spyOn(api, "patch").mockResolvedValue({
      values: { name: "Factory", game_password: MASK, max_players: 8, "visibility.public": true },
      changed: ["max_players", "visibility.public"],
      restart_required: false,
    } as never);
    const onDirty = vi.fn();
    wrap(<GameSettingsTab instance={instance} can={() => true} onDirtyChange={onDirty} />);
    expect(await screen.findByLabelText(/Max players/)).toHaveValue("0");
    expect(screen.queryByLabelText("Port")).toBeNull();
    const user = userEvent.setup();
    await user.clear(screen.getByLabelText(/Max players/));
    await user.type(screen.getByLabelText(/Max players/), "8");
    await user.click(screen.getByRole("switch", { name: /Public listing/ }));
    expect(onDirty).toHaveBeenLastCalledWith(true);
    await user.click(screen.getByRole("button", { name: /^Save$/ }));
    expect(patch).toHaveBeenCalledWith(`/instances/${instance.id}/config/server-settings`, {
      values: { max_players: 8, "visibility.public": true },
    });
  });

  it("sends a secret the user replaced, and refuses invalid numbers before sending", async () => {
    const patch = vi.spyOn(api, "patch").mockResolvedValue({ values: {}, changed: ["game_password"], restart_required: false } as never);
    wrap(<GameSettingsTab instance={instance} can={() => true} onDirtyChange={() => {}} />);
    const user = userEvent.setup();
    const pw = await screen.findByLabelText(/Game password/);
    expect(pw).toHaveValue(MASK);
    await user.clear(pw);
    await user.type(pw, "new-secret");
    await user.clear(screen.getByLabelText(/Max players/));
    await user.type(screen.getByLabelText(/Max players/), "many");
    await user.click(screen.getByRole("button", { name: /^Save$/ }));
    expect(patch).not.toHaveBeenCalled();
    expect(screen.getByText("Enter a whole number")).toBeInTheDocument();
    await user.clear(screen.getByLabelText(/Max players/));
    await user.type(screen.getByLabelText(/Max players/), "0");
    await user.click(screen.getByRole("button", { name: /^Save$/ }));
    expect(patch).toHaveBeenCalledWith(`/instances/${instance.id}/config/server-settings`, { values: { game_password: "new-secret" } });
  });
});

describe("named ports", () => {
  it("validates range and the same port twice per protocol", () => {
    const rows = [
      { name: "api", label: "API", protocol: "tcp" as const, container_port: 7777, host_port: "7777" },
      { name: "game", label: "Game", protocol: "udp" as const, container_port: 7777, host_port: "7777" },
      { name: "msg", label: "Messaging", protocol: "tcp" as const, container_port: 8888, host_port: "7777" },
      { name: "bad", label: "Bad", protocol: "tcp" as const, container_port: 1, host_port: "80" },
    ];
    expect(validateNamedPorts(rows)).toEqual({ msg: "Same port and protocol as API", bad: "A port between 1024 and 65535" });
  });

  it("saves the host port of each named port", async () => {
    const instance = factorio();
    gets["/system/ports"] = { range: [25565, 40000], in_use: [{ port: 34197, used_by: "server 'Factory'" }], suggested: 25566 };
    const patch = vi.spyOn(api, "patch").mockResolvedValue({ ...instance, restart_required: true } as never);
    wrap(<GameNetwork instance={instance} canEdit />);
    expect(screen.getByText("192.0.2.10:34197")).toBeInTheDocument();
    const user = userEvent.setup();
    const input = screen.getByRole("textbox", { name: "Game host port (UDP)" });
    await user.clear(input);
    await user.type(input, "34300");
    await user.click(screen.getByRole("button", { name: /Save/ }));
    expect(patch).toHaveBeenCalledWith(`/instances/${instance.id}`, { ports: [{ name: "game", host_port: 34300 }] });
  });
});

describe("setup needed", () => {
  it("is hidden without setup, offers an automatic step and links a secret step", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ id: "job" } as never);
    const { unmount } = wrap(<SetupCard instance={factorio()} canConfigure />);
    expect(screen.queryByRole("region", { name: "Setup needed" })).toBeNull();
    unmount();
    const steps = [
      { id: "claim", label: "Claim the server", kind: "auto", status: "pending", help: "The panel claims it before ports are published." },
      { id: "token", label: "Cluster token", kind: "secret", status: "pending" },
    ];
    const { unmount: u2 } = wrap(<SetupCard instance={factorio({ setup: { needed: true, next: "claim", steps } })} canConfigure />);
    expect(screen.getByText("Setup needed: Claim the server")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: /Run setup/ }));
    expect(post).toHaveBeenCalledWith(`/instances/${factorio().id}/setup`, { step: "claim" });
    u2();
    wrap(<SetupCard instance={factorio({ setup: { needed: true, next: "token", steps } })} canConfigure />);
    expect(screen.getByRole("link", { name: /Game settings/ })).toHaveAttribute("href", `/servers/${factorio().id}/settings?tab=game-settings`);
  });
});

describe("players of another game", () => {
  it("uses the game's labels and name rule", async () => {
    gets[`/instances/${factorio().id}/players`] = {
      running: true,
      whitelist: [{ name: "alice" }],
      ops: [{ name: "bob.the-builder" }],
      bans: [{ name: "mallory", reason: "griefing" }],
      ip_bans: [],
      known_players: [],
      online: { online: 1, max: null, players: ["alice"] },
      labels: { whitelist: "Whitelist", ops: "Admins", bans: "Bans" },
    };
    const post = vi.spyOn(api, "post").mockResolvedValue({ via: "rcon", output: "Added x" } as never);
    wrap(<GamePlayers instance={factorio()} canManage />);
    expect(await screen.findByRole("heading", { name: "Admins" })).toBeInTheDocument();
    expect(screen.getByText("bob.the-builder")).toBeInTheDocument();
    expect(screen.getByText("griefing")).toBeInTheDocument();
    expect(screen.queryByText(/Player heads/)).toBeNull();
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "Player name to add to Admins" }), "has space");
    await user.click(screen.getByRole("button", { name: /Promote/ }));
    expect(screen.getByText("Not a valid player name for this game")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Kick alice" }));
    expect(post).toHaveBeenCalledWith(`/instances/${factorio().id}/players`, { action: "kick", name: "alice" });
  });

  it("labels: API, then catalog, then defaults; names up to 60 safe characters", () => {
    expect(listLabels({ labels: { ops: "Operators" } }, { ops: "Admins", bans: "Banned" })).toEqual({
      whitelist: "Whitelist",
      ops: "Operators",
      bans: "Banned",
    });
    expect(gamePlayerNameError("x".repeat(60))).toBeNull();
    expect(gamePlayerNameError("x".repeat(61))).toBe("Not a valid player name for this game");
    expect(gamePlayerNameError("", "[a-z]+")).toBe("Enter a player name");
  });
});

describe("saves dialogs", () => {
  it("reset: needs the typed name, sends the seed and the backup choice", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ id: "j" } as never);
    const onStarted = vi.fn();
    wrap(<SavesResetDialog instance={factorio()} fields={factorioGame().world!.reset_fields} onClose={() => {}} onStarted={onStarted} />);
    const user = userEvent.setup();
    const go = screen.getByRole("button", { name: "Start a new map" });
    expect(go).toBeDisabled();
    await user.type(screen.getByLabelText("Map seed"), "4242");
    await user.type(screen.getByLabelText(/to confirm/), "Factory");
    expect(go).toBeEnabled();
    await user.click(go);
    expect(post).toHaveBeenCalledWith(`/instances/${factorio().id}/saves/reset`, { confirm_name: "Factory", seed: 4242, backup: true });
    expect(onStarted).toHaveBeenCalledWith({ id: "j" });
  });

  it("reset: an empty seed means a random map; turning the backup off is said loudly", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ id: "j" } as never);
    wrap(<SavesResetDialog instance={factorio()} onClose={() => {}} onStarted={() => {}} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("switch", { name: "Safety backup first" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/Without a safety backup/);
    await user.type(screen.getByLabelText(/to confirm/), "Factory");
    await user.click(screen.getByRole("button", { name: "Start a new map" }));
    expect(post).toHaveBeenCalledWith(`/instances/${factorio().id}/saves/reset`, { confirm_name: "Factory", seed: null, backup: false });
  });

  it("import: only a .zip, with the typed name, uploaded as a form", async () => {
    const upload = vi.spyOn(apiModule, "uploadWithProgress").mockResolvedValue({ id: "j2" } as never);
    wrap(<SavesImportDialog instance={factorio()} onClose={() => {}} onStarted={() => {}} />);
    const user = userEvent.setup({ applyAccept: false });
    const file = screen.getByLabelText(/Save file/);
    await user.upload(file, new File(["x"], "world.txt"));
    expect(screen.getByText("Choose a .zip file")).toBeInTheDocument();
    await user.upload(file, new File(["PK"], "world.zip"));
    await user.type(screen.getByLabelText(/to confirm/), "Factory");
    await user.click(screen.getByRole("button", { name: "Import" }));
    expect(upload).toHaveBeenCalledTimes(1);
    const [path, form] = upload.mock.calls[0];
    expect(path).toBe(`/instances/${factorio().id}/saves/import`);
    expect((form as FormData).get("confirm_name")).toBe("Factory");
    expect((form as FormData).get("backup")).toBe("true");
    expect(((form as FormData).get("file") as File).name).toBe("world.zip");
  });
});

describe("console of another game", () => {
  it("keeps the leading slash and names the game's stop word", async () => {
    const onSubmit = vi.fn(() => true);
    const rules = consoleRules(factorioGame());
    wrap(<CommandInput instanceId="f1" disabledReason={null} pending={0} onSubmit={onSubmit} rules={rules} />);
    const box = screen.getByRole("textbox", { name: "Console command" });
    expect(box).toHaveAccessibleDescription(/\/quit stops the server through the panel/);
    expect(box).toHaveAttribute("placeholder", "Type a command, e.g. /players online");
    const user = userEvent.setup();
    await user.type(box, "/players online{Enter}");
    expect(onSubmit).toHaveBeenCalledWith("/players online");
  });
});
