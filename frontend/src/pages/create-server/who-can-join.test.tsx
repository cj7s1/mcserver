/**
 * A-9 (security review 2026-11-11): the create wizard's review says who can join the new server once its
 * port is forwarded (docs/GAMES.md "Who can join by default"), for every game the panel knows.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { whoCanJoin } from "@/lib/games";
import { qk } from "@/lib/queries";
import { sixGame, SIX_GAME_IDS } from "@/test/six-games";
import { GameWizard } from "./GameWizard";
import { StepReview } from "./StepReview";
import { initialState, minecraftWhoCanJoin, type Derived } from "./wizardModel";

describe("who can join by default", () => {
  it("has a line for each of the ten games, and none for a game the UI does not know", () => {
    for (const id of ["minecraft", "factorio", "satisfactory", "terraria", ...SIX_GAME_IDS]) expect(whoCanJoin(id), id).toMatch(/\S/);
    expect(whoCanJoin(undefined)).toBe(whoCanJoin("minecraft"));
    expect(whoCanJoin("tetris")).toBeUndefined();
    expect(whoCanJoin("valheim")).toMatch(/join password \(required\)/);
    expect(whoCanJoin("rust")).toMatch(/^Anyone: the panel offers no password or whitelist/);
    expect(whoCanJoin("project-zomboid")).toMatch(/Anyone can create an account/);
  });

  it("Minecraft: what the gameplay step chose", () => {
    expect(minecraftWhoCanJoin({ onlineMode: true, whitelist: false })).toBe(whoCanJoin("minecraft"));
    expect(minecraftWhoCanJoin({ onlineMode: true, whitelist: true })).toMatch(/^Only players on the whitelist/);
    expect(minecraftWhoCanJoin({ onlineMode: false, whitelist: false })).toMatch(/^Anyone, with any user name/);
    expect(minecraftWhoCanJoin({ onlineMode: false, whitelist: true })).toMatch(/anyone can use a whitelisted name/);
  });
});

describe("the Minecraft wizard's review", () => {
  it("says who can join with the chosen whitelist", () => {
    const d = {
      isModpack: false,
      serverType: "PAPER",
      minecraftVersion: "1.21.1",
      loaderVersion: "",
      loaderLabel: null,
      java: null,
      javaVersion: 21,
      name: "Lobby",
      suggestedName: "Lobby",
      memoryMb: 4096,
      defaultMemoryMb: 4096,
      gamePort: "25565",
      ports: undefined,
      resolved: undefined,
      resolveOk: true,
      resolvePending: false,
      softwareSummary: "Paper 1.21.1",
      nodeName: null,
      nightlyBackupAllowed: false,
    } as Derived;
    const state = { ...initialState(), category: "plugins" as const, whitelist: true };
    render(<StepReview state={state} patch={() => {}} d={d} errors={{}} onEdit={() => {}} />);
    expect(screen.getByText("Who can join").closest("p")).toHaveTextContent(
      "Who can join (once its port is forwarded on your router): Only players on the whitelist",
    );
  });
});

describe("the game wizard's review", () => {
  beforeAll(() => {
    globalThis.ResizeObserver ??= class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
    Element.prototype.scrollIntoView ??= function scrollIntoView() {};
    window.scrollTo = (() => {}) as typeof window.scrollTo;
  });
  beforeEach(() => {
    const gets: Record<string, unknown> = {
      "/catalog/games": SIX_GAME_IDS.map(sixGame),
      "/nodes": [],
      "/system/ports": { range: [25565, 40000], in_use: [], suggested: 25565 },
    };
    vi.spyOn(api, "get").mockImplementation(((path: string) =>
      path in gets ? Promise.resolve(structuredClone(gets[path])) : new Promise(() => {})) as never);
  });
  afterEach(() => vi.restoreAllMocks());

  it("Rust: anyone can join", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(qk.auth, { setup_required: false, authenticated: true, user: null });
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <MemoryRouter>
            <GameWizard game={sixGame("rust")} nightlyBackupAllowed onBackToGame={() => {}} />
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    while (!screen.queryByRole("button", { name: /Deploy server/ })) await user.click(screen.getByRole("button", { name: /^(Next|Review)/ }));
    const section = screen.getByRole("heading", { name: "Who can join" }).closest("section")!;
    expect(section).toHaveTextContent(whoCanJoin("rust")!);
    expect(section).toHaveTextContent("Once its port is forwarded on your router");
  });
});
