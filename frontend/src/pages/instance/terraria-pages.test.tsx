/**
 * Instance pages of a Terraria server: the players page shows only the lists the game has (vanilla:
 * bans by IP; TShock: none), the Software tab asks the version catalog of the server's variant, and
 * the console names Terraria's stop words.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { consoleRules, gameLabel, isStopWord, normaliseConsoleCommand, variantLabel } from "@/lib/games";
import { qk } from "@/lib/queries";
import type { GameInfo, Instance, PermissionCode } from "@/lib/types";
import { makeFactorioInstance } from "@/test/fixtures";
import { GamePlayers } from "./players/GamePlayers";
import { GameSoftwareTab } from "./settings/game-software-tab";

const PERMS: PermissionCode[] = ["instance.view", "instance.configure", "instance.players", "instance.command"];

const TERRARIA: GameInfo = {
  id: "terraria",
  label: "Terraria",
  variants: [
    { id: "vanilla", label: "Terraria (vanilla)", default_memory_mb: 1536, notes: [] },
    { id: "tmodloader", label: "tModLoader", default_memory_mb: 3072, notes: [], game_version: "1.4.4.9" },
    { id: "tshock", label: "TShock", default_memory_mb: 1536, notes: [] },
  ],
  capabilities: ["players", "player_actions", "software_update"],
  memory_model: "plain",
  default_memory_mb: 1536,
  notes: [],
  stop_words: ["exit", "exit-nosave", "off", "off-nosave"],
  command_hint: "playing, say Hello, save",
  has_versions: true,
  player_name_pattern: '(?=.{1,20}$)[^\\s"\\\\/][^"\\\\]*(?<!\\s)',
  player_list_labels: { bans: "Bans (by IP)" },
};

function terraria(over: Partial<Instance> = {}): Instance {
  return makeFactorioInstance({
    id: "33333333-3333-3333-3333-333333333333",
    name: "Terra",
    game: "terraria",
    game_label: "Terraria",
    server_type: "tshock" as Instance["server_type"],
    minecraft_version: "1.4.5.8-6.2.1",
    image: "ryshe/terraria:tshock-1.4.5.8-6.2.1",
    capabilities: ["players", "player_actions", "software_update", "query"],
    ports: [{ name: "game", label: "Game", protocol: "tcp", container_port: 7777, host_port: 7777, host_ip: "0.0.0.0" }],
    game_options: { world_name: "world", owner_account: "owner" },
    permissions: PERMS,
    ...over,
  });
}

let gets: Record<string, unknown> = {};
let queries: { path: string; query: unknown }[] = [];

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Element.prototype.scrollIntoView ??= function scrollIntoView() {};
});

beforeEach(() => {
  queries = [];
  gets = { "/catalog/games": [TERRARIA] };
  vi.spyOn(api, "get").mockImplementation(((path: string, query?: unknown) => {
    queries.push({ path, query });
    return path in gets ? Promise.resolve(structuredClone(gets[path])) : new Promise(() => {});
  }) as never);
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

const players = (over: Record<string, unknown>) => ({
  running: true,
  whitelist: [],
  ops: [],
  bans: [],
  ip_bans: [],
  known_players: [],
  online: { online: 1, max: 8, players: ["Ann Lee"] },
  ...over,
});

describe("players of a Terraria server", () => {
  it("vanilla: only the ban list (by IP), names with spaces allowed", async () => {
    const inst = terraria({ server_type: "vanilla" as Instance["server_type"] });
    gets[`/instances/${inst.id}/players`] = players({ bans: [{ name: "Troll", reason: "" }], labels: { bans: "Bans (by IP)" } });
    wrap(<GamePlayers instance={inst} canManage />);
    expect(await screen.findByRole("heading", { name: "Bans (by IP)" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Whitelist" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Admins" })).toBeNull();
    expect(screen.getByText("Ann Lee")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Kick Ann Lee" })).toBeInTheDocument();
  });

  it("TShock: no lists (TShock keeps them itself), online players with kick", async () => {
    const inst = terraria();
    gets[`/instances/${inst.id}/players`] = players({ labels: {} });
    wrap(<GamePlayers instance={inst} canManage />);
    expect(await screen.findByText("Ann Lee")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Bans|Whitelist|Admins/ })).toBeNull();
  });
});

describe("software of a Terraria server", () => {
  it("lists the versions of the server's own variant", async () => {
    const inst = terraria();
    gets["/catalog/games/terraria/versions"] = {
      versions: [{ id: "1.4.5.8-6.2.1", label: "TShock 6.2.1 (Terraria 1.4.5.8)", stable: true, latest: true }],
      latest: "1.4.5.8-6.2.1",
    };
    wrap(<GameSoftwareTab instance={inst} can={() => true} onDirtyChange={() => {}} />);
    await screen.findAllByText(/TShock 6\.2\.1/);
    expect(queries).toContainEqual({ path: "/catalog/games/terraria/versions", query: { variant: "tshock" } });
  });
});

describe("Terraria labels and console", () => {
  it("names the game and its variants", () => {
    expect(gameLabel("terraria")).toBe("Terraria");
    expect(variantLabel("tmodloader")).toBe("tModLoader");
    expect(variantLabel("tshock")).toBe("TShock");
  });

  it("strips the slash and turns exit/off into the panel's Stop", () => {
    const rules = consoleRules(TERRARIA);
    expect(rules.keepsSlash).toBe(false);
    expect(normaliseConsoleCommand("/playing", rules)).toBe("playing");
    expect(isStopWord("exit", rules)).toBe(true);
    expect(isStopWord("OFF", rules)).toBe(true);
    expect(isStopWord("say exit", rules)).toBe(false);
    expect(rules.example).toBe("playing, say Hello, save");
  });
});
