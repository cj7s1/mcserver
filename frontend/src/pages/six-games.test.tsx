/**
 * The pages of the six Steam-era games (Palworld, Valheim, Project Zomboid, HKMP, CS2, Rust) with the
 * backend's own catalog entries (src/test/six-games.ts): the create wizard (derived and conditional
 * ports, empty enum options, generated secrets, links), Game settings (secrets that can be shown or not
 * cleared, patterns, the fixed software), Network (ports that follow another port, 1:1 ports, ports
 * published only while a setting is on), the saves of a world folder and the players page shapes.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { ConfigValue, Instance, PermissionCode, PlayersResponse } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { sixGame, SIX_GAME_IDS, type SixGameId } from "@/test/six-games";
import { GameWizard } from "./create-server/GameWizard";
import { SavesImportDialog, SavesResetDialog } from "./instance/backups/SavesDialogs";
import { SavesSection } from "./instance/backups/SavesSection";
import { InstanceLayout } from "./instance/InstanceLayout";
import { ConnectionPorts, GameNetwork } from "./instance/network/GameNetwork";
import Overview from "./instance/Overview";
import { SoftwareCard } from "./instance/overview/info-cards";
import { GamePlayers } from "./instance/players/GamePlayers";
import Settings from "./instance/Settings";
import { GameSettingsTab } from "./instance/settings/game-settings-tab";

const ALL_PERMS: PermissionCode[] = [
  "instance.view",
  "instance.control",
  "instance.console",
  "instance.command",
  "instance.configure",
  "instance.files.read",
  "instance.files.write",
  "instance.players",
  "instance.backup",
  "instance.restore",
  "instance.schedules",
  "instance.access",
  "instance.delete",
];

/** A server of one of the six games as the backend sends it: virtual configs with their defaults in game_options, 1:1 ports. */
function sixInstance(id: SixGameId, over: Partial<Instance> = {}): Instance {
  const game = sixGame(id);
  const options: Record<string, ConfigValue | Record<string, ConfigValue>> = {};
  for (const o of game.options ?? []) options[o.key] = o.default ?? null;
  for (const f of game.config_files ?? [])
    if (f.path === "(container settings)") options[f.id] = Object.fromEntries(f.fields.map((x) => [x.key, x.default ?? null]));
  const ports = (game.ports ?? []).map((p) => ({
    name: p.name,
    label: p.label,
    protocol: p.protocol,
    container_port: p.container_port,
    host_port: p.default_host_port,
    host_ip: "0.0.0.0",
  }));
  return makeInstance({
    id: "66666666-6666-6666-6666-666666666666",
    name: `${game.label} test`,
    server_type: "" as Instance["server_type"],
    minecraft_version: game.fixed_version!.id,
    java_version: 0,
    image: `example/${id}:1`,
    memory_mb: game.default_memory_mb,
    memory_overhead_mb: 0,
    container_memory_mb: game.default_memory_mb,
    game_port: ports[0].host_port,
    game: id,
    game_label: game.label,
    capabilities: game.capabilities,
    memory_model: "plain",
    ports,
    game_options: options,
    setup: null,
    permissions: ALL_PERMS,
    connection: {
      host_port: ports[0].host_port,
      container_port: ports[0].container_port,
      bind_address: "0.0.0.0",
      addresses: [],
      notes: [],
      ports: ports.map((p) => ({ ...p, addresses: [{ kind: "lan" as const, label: "LAN", address: `192.0.2.10:${p.host_port}` }] })),
    },
    ...over,
  });
}

let gets: Record<string, unknown> = {};
let post: ReturnType<typeof vi.fn>;

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Element.prototype.scrollIntoView ??= function scrollIntoView() {};
  Element.prototype.hasPointerCapture ??= function hasPointerCapture() {
    return false;
  };
  window.scrollTo = (() => {}) as typeof window.scrollTo;
});

beforeEach(() => {
  gets = {
    "/catalog/games": SIX_GAME_IDS.map(sixGame),
    "/nodes": [],
    "/system/ports": { range: [25565, 40000], in_use: [], suggested: 25565 },
  };
  vi.spyOn(api, "get").mockImplementation(((path: string) =>
    path in gets ? Promise.resolve(structuredClone(gets[path])) : new Promise(() => {})) as never);
  post = vi.fn(async () => ({}));
  vi.spyOn(api, "post").mockImplementation(post as never);
});
afterEach(() => vi.restoreAllMocks());

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(qk.auth, { setup_required: false, authenticated: true, user: null });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter>{children}</MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("sections of the six games", () => {
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

  it.each(SIX_GAME_IDS)("%s: the server's tabs and Settings tabs come from its capabilities", (id) => {
    renderAt(sixInstance(id), "settings");
    const nav = screen.getByRole("navigation", { name: "Server sections" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent?.trim())).toEqual([
      "Overview",
      "Console",
      "Files",
      "Players",
      "Backups",
      "Schedules",
      "Network",
      "Settings",
      "Access",
      "Activity",
    ]);
    const list = screen.getByRole("tablist", { name: "Settings sections" });
    // no version to pick (no Software tab), plain memory (no JVM)
    expect(within(list).getAllByRole("tab").map((t) => t.textContent?.trim())).toEqual(["Game settings", "Resources", "Environment", "General", "Danger zone"]);
  });
});

describe("create wizard", () => {
  const renderWizard = (id: SixGameId) => wrap(<GameWizard game={sixGame(id)} nightlyBackupAllowed onBackToGame={() => {}} />);
  const next = async (user: ReturnType<typeof userEvent.setup>) => user.click(screen.getByRole("button", { name: /^(Next|Review)/ }));

  it("Valheim: one port to choose, query and crossplay follow it; the preset's empty option; the join password", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue({ instance: sixInstance("valheim"), job: { id: "j1" }, warnings: [], java: null });
    renderWizard("valheim");
    expect(screen.getByText("Steam public branch")).toBeInTheDocument();
    expect(screen.getByLabelText("World name")).toHaveValue("Dedicated");
    await next(user);

    // only the game port is asked for; the others are listed with their rule
    const portInputs = screen.getAllByRole("textbox").filter((i) => i.id.startsWith("game-port-"));
    expect(portInputs.map((i) => i.id)).toEqual(["game-port-game"]);
    const following = screen.getByRole("list", { name: "Ports that follow another port" });
    expect(within(following).getByText("Steam query")).toBeInTheDocument();
    expect(within(following).getByText("Game port + 1")).toBeInTheDocument();
    expect(within(following).getByText("only while “Crossplay (Xbox and Microsoft Store players)” is on")).toBeInTheDocument();
    await user.type(portInputs[0], "2500");
    expect(within(following).getByText("Game port + 1 = 2501")).toBeInTheDocument();
    expect(within(following).getByText("Game port + 2 = 2502")).toBeInTheDocument();
    await next(user);

    // an enum whose empty option is the game's default renders (Radix items cannot have the value "")
    expect(screen.getByRole("combobox", { name: "World preset" })).toHaveTextContent("Default");
    await next(user);

    expect(screen.getByText("Crossplay (UDP)").nextSibling).toHaveTextContent("2502, not published: only while “Crossplay (Xbox and Microsoft Store players)” is on");
    await user.click(screen.getByRole("button", { name: /Deploy server/ }));
    expect(screen.getByText("Join password is required")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText(/Join password/), "Odin1");
    await user.click(screen.getByRole("button", { name: /Deploy server/ }));
    expect(post).toHaveBeenCalledTimes(1);
    const body = post.mock.calls[0][1] as Record<string, unknown>;
    expect(body).toMatchObject({ game: "valheim", version: "public", ports: { game: 2500 }, secrets: { server_password: "Odin1" }, options: { world_name: "Dedicated" } });
  });

  it("Palworld: the admin password is generated when left empty", async () => {
    const user = userEvent.setup();
    post.mockResolvedValue({ instance: sixInstance("palworld"), job: { id: "j1" }, warnings: [], java: null });
    renderWizard("palworld");
    await next(user);
    await next(user);
    // a server name the image cannot take is refused before the request
    const name = screen.getByLabelText("Server name");
    await user.clear(name);
    await user.type(name, 'Pal "world"');
    await next(user);
    expect(screen.getByText(/^Allowed: 1 to 64 letters/)).toBeInTheDocument();
    await user.clear(name);
    await user.type(name, "Pals together");
    await next(user);
    const admin = screen.getByLabelText(/Admin password/);
    expect(admin).toHaveAttribute("placeholder", "Generated if left empty");
    expect(screen.getByText(/Leave it empty and the panel generates one\./)).toBeInTheDocument();
    expect(screen.getByText(/the passwords players type in game can be shown/)).toBeInTheDocument();
    await user.type(admin, "short");
    await user.click(screen.getByRole("button", { name: /Deploy server/ }));
    expect(screen.getByText(/^Allowed: 12 to 64 letters/)).toBeInTheDocument();
    await user.clear(admin);
    await user.click(screen.getByRole("button", { name: /Deploy server/ }));
    const body = post.mock.calls[0][1] as Record<string, unknown>;
    expect(body.secrets).toBeUndefined();
    expect(body.settings).toEqual({ server: { server_name: "Pals together" } });
  });

  it("CS2: the CSTV port says when it is published; the GSLT links to Steam", async () => {
    const user = userEvent.setup();
    renderWizard("cs2");
    await next(user);
    expect(screen.getByText("only while “CSTV (spectators)” is on")).toBeInTheDocument();
    expect(screen.getAllByRole("textbox").filter((i) => i.id.startsWith("game-port-")).map((i) => i.id)).toEqual(["game-port-game", "game-port-tv"]);
    await next(user);
    await next(user);
    expect(screen.getByRole("link", { name: "Where to get it" })).toHaveAttribute("href", "https://steamcommunity.com/dev/managegameservers");
    await user.type(screen.getByLabelText(/Game Server Login Token/), "not-a-token");
    await user.click(screen.getByRole("button", { name: /Deploy server/ }));
    expect(screen.getByText("Allowed: 32 characters, digits and A-F (as Steam shows it)")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });
});

describe("Game settings", () => {
  const secretsOf = (id: SixGameId, set: Record<string, boolean>) => ({
    secrets: (sixGame(id).secrets ?? []).map((s) => ({ ...s, set: !!set[s.key] })),
  });

  it("Palworld: the admin password can be shown but not cleared; the join password can be cleared; patterns are checked", async () => {
    const inst = sixInstance("palworld", { status: "running" });
    gets[`/instances/${inst.id}/config`] = { files: [] };
    gets[`/instances/${inst.id}/secrets`] = secretsOf("palworld", { admin_password: true, server_password: true });
    post.mockResolvedValue({ key: "admin_password", value: "Generated-Admin-42" });
    const user = userEvent.setup();
    wrap(<GameSettingsTab instance={inst} can={() => true} onDirtyChange={() => {}} />);
    const admin = (await screen.findByLabelText(/^Admin password/)).closest("li")!;
    const join = screen.getByLabelText(/^Join password/).closest("li")!;
    expect(within(admin).queryByRole("button", { name: "Clear" })).toBeNull();
    expect(within(join).getByRole("button", { name: "Clear" })).toBeInTheDocument();
    expect(within(join).queryByRole("button", { name: /Show/ })).toBeNull();

    await user.click(within(admin).getByRole("button", { name: /Show/ }));
    expect(post).toHaveBeenCalledWith(`/instances/${inst.id}/secrets/admin_password/reveal`);
    expect(await within(admin).findByLabelText("Admin password (shown)")).toHaveTextContent("Generated-Admin-42");
    await user.click(within(admin).getByRole("button", { name: "Hide the admin password" }));
    expect(within(admin).queryByLabelText("Admin password (shown)")).toBeNull();

    await user.type(within(join).getByLabelText(/^Join password/), "has space");
    expect(within(join).getByRole("alert")).toHaveTextContent(/^Allowed: 1 to 64 letters/);
    expect(within(join).getByRole("button", { name: "Save" })).toBeDisabled();

    // no version to pick: the branch, updated by a restart
    const software = screen.getByRole("heading", { name: "Software" }).closest("section")!;
    expect(within(software).getByText("Steam public branch")).toBeInTheDocument();
    expect(within(software).getByRole("button", { name: /Update now/ })).toBeInTheDocument();
  });

  it("HKMP: its version comes with the panel's image: no 'Update now'", async () => {
    const inst = sixInstance("hkmp", { status: "running" });
    gets[`/instances/${inst.id}/config`] = { files: [] };
    wrap(<GameSettingsTab instance={inst} can={() => true} onDirtyChange={() => {}} />);
    const software = (await screen.findByRole("heading", { name: "Software" })).closest("section")!;
    expect(within(software).getByText("HKMP 2.4.3")).toBeInTheDocument();
    expect(within(software).queryByRole("button", { name: /Update now/ })).toBeNull();
  });

  it("Valheim: a virtual config with empty enum options renders and saves", async () => {
    const inst = sixInstance("valheim");
    const server = sixGame("valheim").config_files![0];
    gets[`/instances/${inst.id}/config`] = {
      files: [{ ...server, exists: true, values: (inst.game_options as Record<string, Record<string, ConfigValue>>).server, redacted_keys: [] }],
    };
    gets[`/instances/${inst.id}/secrets`] = secretsOf("valheim", { server_password: true });
    const patch = vi.spyOn(api, "patch").mockResolvedValue({ values: {}, changed: ["preset"], restart_required: false } as never);
    const user = userEvent.setup();
    wrap(<GameSettingsTab instance={inst} can={() => true} onDirtyChange={() => {}} />);
    const preset = await screen.findByRole("combobox", { name: "World preset" });
    expect(preset).toHaveTextContent("Default");
    await user.click(preset);
    await user.click(await screen.findByRole("option", { name: "hard" }));
    const form = screen.getByRole("heading", { name: "Server" }).closest("section")!;
    await user.click(within(form).getByRole("button", { name: /Save/ }));
    expect(patch).toHaveBeenCalledWith(`/instances/${inst.id}/config/server`, { values: { preset: "hard" } });
    // the required join password cannot be cleared
    const join = screen.getByLabelText(/^Join password/).closest("li")!;
    expect(within(join).queryByRole("button", { name: "Clear" })).toBeNull();
  });
});

describe("Network", () => {
  it("Valheim: the game port is edited, query and crossplay follow it; 1:1 ports; crossplay is off", async () => {
    const inst = sixInstance("valheim", {
      ports: [
        { name: "game", label: "Game", protocol: "udp", container_port: 2456, host_port: 2600, host_ip: "0.0.0.0" },
        { name: "query", label: "Steam query", protocol: "udp", container_port: 2457, host_port: 2601, host_ip: "0.0.0.0" },
        { name: "crossplay", label: "Crossplay", protocol: "udp", container_port: 2458, host_port: 2602, host_ip: "0.0.0.0" },
      ],
    });
    inst.connection.ports = inst.ports!.map((p) => ({ ...p, label: p.label ?? p.name, addresses: [] }));
    const user = userEvent.setup();
    wrap(<GameNetwork instance={inst} canEdit />);
    // (the catalog says which ports follow another one)
    const following = await screen.findByRole("list", { name: "Ports that follow another port" });
    const editor = screen.getByRole("list", { name: "Ports" });
    expect(within(editor).getAllByRole("textbox").map((i) => i.getAttribute("aria-label"))).toEqual(["Game host port (UDP)"]);
    expect(within(editor).getByText(/Published 1:1/)).toBeInTheDocument();
    expect(within(following).getByText("2601")).toBeInTheDocument();
    expect(within(following).getByText("Not published now: only while “Crossplay (Xbox and Microsoft Store players)” is on")).toBeInTheDocument();
    // a 1:1 port listens on the host port's number inside the container
    expect(screen.getAllByText("host 2600 → 2600")).toHaveLength(1);
    const game = within(editor).getByRole("textbox", { name: "Game host port (UDP)" });
    await user.clear(game);
    await user.type(game, "65534");
    expect(within(editor).getByRole("alert")).toHaveTextContent("Crossplay would be port 65536; choose a lower port");
    await user.clear(game);
    await user.type(game, "3000");
    expect(within(following).getByText("3001")).toBeInTheDocument();
    const patch = vi.spyOn(api, "patch").mockResolvedValue(inst as never);
    await user.click(screen.getByRole("button", { name: /Save/ }));
    expect(patch).toHaveBeenCalledWith(`/instances/${inst.id}`, { ports: [{ name: "game", host_port: 3000 }] });
  });

  it("CS2: the CSTV port is edited and says whether it is published", async () => {
    const off = sixInstance("cs2");
    const { unmount } = wrap(<ConnectionPorts instance={off} />);
    expect(await screen.findByText("Not published now: only while “CSTV (spectators)” is on")).toBeInTheDocument();
    unmount();
    const on = sixInstance("cs2");
    (on.game_options!.server as Record<string, ConfigValue>).cstv = true;
    wrap(<ConnectionPorts instance={on} />);
    expect(await screen.findByText("Published now (only while “CSTV (spectators)” is on)")).toBeInTheDocument();
  });
});

describe("saves of a world folder", () => {
  it("Rust: the identity folder, a partly measured size, a reset with blueprints", async () => {
    const inst = sixInstance("rust");
    gets[`/instances/${inst.id}/saves`] = {
      current: "rust",
      saves: [{ name: "rust", path: "server/rust", type: "directory", size: 3 * 1024 ** 3, size_complete: false, modified: 1_700_000_000, current: true }],
      set_aside: [{ folder: "saves-replaced/20261110T120000Z", files: ["proceduralmap.3500.1.map"] }],
      options: { identity: "rust" },
      reset_fields: sixGame("rust").world!.reset_fields,
      import_help: sixGame("rust").world!.import_help,
      notes: ["A world reset is a map wipe: the map, the saves and the world's databases are moved aside."],
      max_upload_bytes: null,
    };
    post.mockResolvedValue({ id: "job-1" });
    const user = userEvent.setup();
    wrap(<SavesSection instance={inst} canChange canDownload tracked={{ job: null, track: () => {} } as never} showTracked={false} />);
    const world = await screen.findByRole("list", { name: "World" });
    expect(within(world).getByText("current world")).toBeInTheDocument();
    expect(within(world).getByText(/^≥ 3(\.0)? GB/)).toBeInTheDocument();
    expect(screen.getByText(/A world reset is a map wipe/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Download/ })).toHaveAttribute("href", expect.stringContaining(`/instances/${inst.id}/saves/export`));

    await user.click(screen.getByRole("button", { name: /New world/ }));
    const dialog = await screen.findByRole("dialog", { name: "Start a new world?" });
    expect(within(dialog).getByText(/saves-replaced\//)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("switch", { name: "Also wipe blueprints" }));
    await user.type(within(dialog).getByLabelText(/to confirm/), inst.name);
    await user.click(within(dialog).getByRole("button", { name: "Start a new world" }));
    expect(post).toHaveBeenCalledWith(`/instances/${inst.id}/saves/reset`, {
      confirm_name: inst.name,
      seed: null,
      backup: true,
      settings: { wipe_blueprints: true },
    });
  });

  it("Palworld: a reset asks for nothing but the confirmation (no seed field)", () => {
    wrap(<SavesResetDialog instance={sixInstance("palworld")} fields={[]} folder={{ setAside: "saves-replaced" }} onClose={() => {}} onStarted={() => {}} />);
    expect(screen.getByRole("dialog", { name: "Start a new world?" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/seed/i)).toBeNull();
  });

  it("import: says what the zip must hold and refuses a file larger than the largest world", async () => {
    const user = userEvent.setup();
    const help = sixGame("valheim").world!.import_help!;
    wrap(<SavesImportDialog instance={sixInstance("valheim")} folder={{ help, maxBytes: 1000, maxUpload: null }} onClose={() => {}} onStarted={() => {}} />);
    const dialog = screen.getByRole("dialog", { name: "Import a world" });
    expect(within(dialog).getByText(new RegExp(help.slice(0, 30).replace(/[()]/g, ".")))).toBeInTheDocument();
    await user.upload(within(dialog).getByLabelText(/World \(\.zip of the world folder\)/), new File([new Uint8Array(2000)], "world.zip", { type: "application/zip" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("The file is larger than the panel accepts");
    await user.type(within(dialog).getByLabelText(/to confirm/), sixInstance("valheim").name);
    expect(within(dialog).getByRole("button", { name: "Import" })).toBeDisabled();
  });
});

describe("players", () => {
  const base = (over: Partial<PlayersResponse>): PlayersResponse => ({
    running: true,
    whitelist: [],
    ops: [],
    bans: [],
    ip_bans: [],
    known_players: [],
    ...over,
  });

  it("Palworld: online players with their ids; kick and ban by id; only the ban list", async () => {
    const inst = sixInstance("palworld", { status: "running" });
    gets[`/instances/${inst.id}/players`] = base({
      labels: { bans: "Bans (user ids)" },
      bans: [{ name: "steam_76561198000000009", reason: "" }],
      id_hint: "Bans and kicks use the player's user id (steam_<SteamID64>, shown with the online players).",
      online: { online: 1, max: 16, players: ["Pal Fan"], entries: [{ name: "Pal Fan", id: "steam_76561198000000001" }] },
    });
    post.mockResolvedValue({ via: "api", output: "done" });
    const user = userEvent.setup();
    wrap(<GamePlayers instance={inst} canManage />);
    expect(await screen.findByText("steam_76561198000000001")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Bans (user ids)" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Whitelist|Admins/ })).toBeNull();
    expect(screen.getByText(/Bans and kicks use the player's user id/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Kick Pal Fan" }));
    expect(post).toHaveBeenLastCalledWith(`/instances/${inst.id}/players`, { action: "kick", name: "steam_76561198000000001" });
    await user.click(screen.getByRole("button", { name: "Ban Pal Fan" }));
    await user.click(await screen.findByRole("button", { name: "Ban player" }));
    expect(post).toHaveBeenLastCalledWith(`/instances/${inst.id}/players`, { action: "ban", name: "steam_76561198000000001" });
  });

  it("Valheim: no online list; the lists are files of SteamID64s", async () => {
    const inst = sixInstance("valheim", { status: "running" });
    gets[`/instances/${inst.id}/players`] = base({
      labels: { whitelist: "Permitted (whitelist)", ops: "Admins", bans: "Banned" },
      ops: [{ name: "76561198000000001" }],
      no_online_list: true,
      id_hint: "Valheim's lists hold SteamID64s (17 digits, e.g. from steamid.io).",
    });
    wrap(<GamePlayers instance={inst} canManage />);
    expect(await screen.findByText("Valheim has no way to list the players online.")).toBeInTheDocument();
    expect(screen.getByText(/These lists are files the server reads when it starts/)).toBeInTheDocument();
    expect(screen.getAllByRole("heading").map((h) => h.textContent)).toEqual(expect.arrayContaining(["Permitted (whitelist)", "Admins", "Banned"]));
    expect(screen.getAllByText("Valheim's lists hold SteamID64s (17 digits, e.g. from steamid.io).")).toHaveLength(3);
  });

  it("Project Zomboid: lists the panel cannot read are changed by name (add and remove)", async () => {
    const inst = sixInstance("project-zomboid", { status: "running" });
    gets[`/instances/${inst.id}/players`] = base({
      labels: { whitelist: "Whitelist (accounts)", ops: "Admins", bans: "Bans" },
      lists_unknown: true,
      id_hint: "Project Zomboid keeps accounts, admins and bans in its database: the panel changes them while the server runs but cannot list them.",
      online: { online: 0, max: 16, players: [] },
    });
    post.mockResolvedValue({ via: "rcon", output: "ok" });
    const user = userEvent.setup();
    wrap(<GamePlayers instance={inst} canManage />);
    const list = (await screen.findByRole("heading", { name: "Whitelist (accounts)" })).closest("section")!;
    expect(within(list).getByText("Not readable by the panel")).toBeInTheDocument();
    await user.type(within(list).getByRole("textbox", { name: "Player name to add to Whitelist (accounts)" }), "Survivor");
    await user.click(within(list).getByRole("button", { name: /Remove/ }));
    expect(post).toHaveBeenCalledWith(`/instances/${inst.id}/players`, { action: "whitelist_remove", name: "Survivor" });
  });

  it("HKMP: IP bans are listed with the bans and lifted by address; Rust: owners by SteamID with their names", async () => {
    const hkmp = sixInstance("hkmp", { status: "running" });
    gets[`/instances/${hkmp.id}/players`] = base({
      labels: { whitelist: "Whitelist", ops: "Authorized (admins)", bans: "Bans" },
      ip_bans: [{ ip: "192.0.2.7", reason: "", name: "" }],
      online: { online: 0, max: null, players: [] },
    });
    post.mockResolvedValue({ via: "console", output: "ok" });
    const user = userEvent.setup();
    const { unmount } = wrap(<GamePlayers instance={hkmp} canManage />);
    expect(await screen.findByText("IP address 192.0.2.7")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Unban 192.0.2.7" }));
    expect(post).toHaveBeenLastCalledWith(`/instances/${hkmp.id}/players`, { action: "pardon", name: "192.0.2.7" });
    unmount();

    const rust = sixInstance("rust", { status: "running" });
    gets[`/instances/${rust.id}/players`] = base({
      labels: { ops: "Owners (admins)", bans: "Bans" },
      ops: [{ name: "76561198000000001", display_name: "Chief" }],
      online: { online: 0, max: 50, players: [], entries: [] },
    });
    wrap(<GamePlayers instance={rust} canManage />);
    expect(await screen.findByText("Chief")).toBeInTheDocument();
    expect(screen.getByText("76561198000000001")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Demote Chief" }));
    expect(post).toHaveBeenLastCalledWith(`/instances/${rust.id}/players`, { action: "deop", name: "76561198000000001" });
  });

  it("CS2: no lists, kicks only", async () => {
    const inst = sixInstance("cs2", { status: "running" });
    gets[`/instances/${inst.id}/players`] = base({ labels: {}, online: { online: 1, max: 10, players: ["Terrorist"] } });
    wrap(<GamePlayers instance={inst} canManage />);
    expect(await screen.findByRole("button", { name: "Kick Terrorist" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Whitelist|Admins|Bans/ })).toBeNull();
  });
});

describe("Overview", () => {
  it("Project Zomboid: the branch chosen at creation and the pinned server name", async () => {
    const inst = sixInstance("project-zomboid", { status: "running", minecraft_version: "legacy41" });
    inst.game_options = { ...inst.game_options, branch: "legacy41" };
    wrap(<SoftwareCard instance={inst} canConfigure />);
    expect(await screen.findByText("Build 41 (legacy41)")).toBeInTheDocument();
    expect(screen.getByText("servertest")).toBeInTheDocument();
    expect(screen.getByText("Steam branch chosen at creation")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Update now/ })).toBeInTheDocument();
  });
});
