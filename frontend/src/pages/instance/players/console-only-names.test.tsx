/**
 * C-6 (security review 2026-11-11): online players under names the panel cannot act on are shown with
 * "use the console" instead of being dropped (Minecraft: `online.other_names`, nicknames and Bedrock
 * players) or offered a Kick the panel refuses (CS2: a name with a quote or ";").
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Instance, PlayersResponse } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { sixGame, SIX_GAME_IDS } from "@/test/six-games";
import { GamePlayers } from "./GamePlayers";
import { OnlinePlayers } from "./OnlinePlayers";
import type { McPlayers } from "./player-utils";

const MC_ID = "88888888-8888-8888-8888-888888888888";

function mc(online: McPlayers["online"]): McPlayers {
  return { running: true, whitelist: [], ops: [], bans: [], ip_bans: [], known_players: [], online };
}

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(qk.auth, { setup_required: false, authenticated: true, user: null });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Minecraft: names in the list that are no account (other_names)", () => {
  it("lists them with use the console and no actions, next to the players the panel can act on", () => {
    wrap(<OnlinePlayers instanceId={MC_ID} data={mc({ online: 3, max: 20, players: ["Notch"], other_names: ["~Notch", ".BedrockSteve"] })} canManage showHeads={false} />);
    const others = screen.getByRole("list", { name: "Online players the panel cannot act on" });
    expect(within(others).getByText("~Notch")).toBeInTheDocument();
    expect(within(others).getByText(".BedrockSteve")).toBeInTheDocument();
    expect(within(others).getAllByText("use the console")).toHaveLength(2);
    expect(within(others).queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "console" })).toHaveAttribute("href", `/servers/${MC_ID}/console`);
    // the account Notch keeps its buttons; the nickname ~Notch is not taken for it
    expect(screen.getAllByRole("button", { name: /Kick/ })).toHaveLength(1);
    expect(screen.getByText("3 / 20 online")).toBeInTheDocument();
  });

  it("only such names online: they are shown instead of “Nobody is online”", () => {
    wrap(<OnlinePlayers instanceId={MC_ID} data={mc({ online: 1, max: 20, players: [], other_names: [".Steve"] })} canManage showHeads={false} />);
    expect(screen.queryByText("Nobody is online right now.")).not.toBeInTheDocument();
    expect(screen.getByText(".Steve")).toBeInTheDocument();
  });

  it("an answer without the field is shown as before", () => {
    wrap(<OnlinePlayers instanceId={MC_ID} data={mc({ online: 0, max: 20, players: [] })} canManage showHeads={false} />);
    expect(screen.getByText("Nobody is online right now.")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Online players the panel cannot act on" })).not.toBeInTheDocument();
  });
});

describe("other games: a name the game's commands cannot carry", () => {
  const CS2: Instance = makeInstance({
    id: "99999999-9999-9999-9999-999999999999",
    name: "CS2 test",
    game: "cs2",
    game_label: "Counter-Strike 2",
    capabilities: sixGame("cs2").capabilities,
    memory_model: "plain",
    status: "running",
  });
  let gets: Record<string, unknown>;
  beforeEach(() => {
    gets = { "/catalog/games": SIX_GAME_IDS.map(sixGame) };
    vi.spyOn(api, "get").mockImplementation(((path: string) =>
      path in gets ? Promise.resolve(structuredClone(gets[path])) : new Promise(() => {})) as never);
  });
  afterEach(() => vi.restoreAllMocks());

  it("CS2: Kick for a plain name, use the console for one with a quote or a semicolon", async () => {
    const answer: PlayersResponse = {
      running: true,
      whitelist: [],
      ops: [],
      bans: [],
      ip_bans: [],
      known_players: [],
      labels: {},
      online: { online: 3, max: 10, players: ["Terrorist", 'The "Boss"', "semi;colon"] },
    };
    gets[`/instances/${CS2.id}/players`] = answer;
    wrap(<GamePlayers instance={CS2} canManage />);
    expect(await screen.findByRole("button", { name: "Kick Terrorist" })).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Online players" });
    // (once the catalog's name pattern is known; before it, nothing is flagged)
    expect(await within(list).findAllByText("use the console")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /Kick The "Boss"/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Kick semi;colon" })).not.toBeInTheDocument();
  });
});
