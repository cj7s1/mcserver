import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { AuthState, GameInfo } from "@/lib/types";
import CreateServer from "../CreateServer";

const MINECRAFT: GameInfo = {
  id: "minecraft",
  label: "Minecraft",
  variants: [
    { id: "VANILLA", label: "Vanilla" },
    { id: "PAPER", label: "Paper" },
  ],
  capabilities: ["console_commands", "jvm", "players", "properties"],
  memory_model: "jvm",
  default_memory_mb: 4096,
  notes: ["Java edition.", "4 GB suits a small server."],
};
const FACTORIO: GameInfo = {
  id: "factorio",
  label: "Factorio",
  variants: [],
  capabilities: ["console_commands"],
  memory_model: "plain",
  default_memory_mb: 2048,
  notes: "Needs a factorio.com account for the server list.",
};
/** Today's wizard (before the Game step existed). */
const TODAY_STEPS = ["Type", "Software", "Resources", "Gameplay", "Review"];

const AUTH: AuthState = {
  setup_required: false,
  authenticated: true,
  user: {
    id: 1,
    username: "owner",
    email: "",
    first_name: "",
    last_name: "",
    is_owner: true,
    role_name: null,
    permissions: ["instance.create", "instance.view"],
    must_change_password: false,
    preferences: {},
  },
};

afterEach(() => vi.restoreAllMocks());

function setup(games: () => Promise<unknown>, path = "/servers/new") {
  vi.spyOn(api, "get").mockImplementation(((p: string) => (p === "/catalog/games" ? games() : new Promise(() => {}))) as never);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(qk.auth, AUTH);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <CreateServer />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

function stepNames(): string[] {
  const nav = screen.getByRole("navigation", { name: "Create server progress" });
  return within(nav)
    .getAllByRole("button")
    .map((b) => b.textContent?.replace(/^\d+|\((completed|current)\)$/g, "").trim() ?? "");
}

describe("create wizard: Game step", () => {
  it.each([
    ["one game", () => Promise.resolve([MINECRAFT])],
    ["the list wrapped in an object", () => Promise.resolve({ games: [MINECRAFT] })],
    ["an older panel (404)", () => Promise.reject(new ApiError(404, "not_found", "Not found."))],
    ["the list still loading", () => new Promise(() => {})],
  ])("is skipped with %s: the wizard is today's", async (_name, games) => {
    setup(games);
    expect(await screen.findByText("What kind of server?")).toBeInTheDocument();
    expect(stepNames()).toEqual(TODAY_STEPS);
    expect(screen.queryByText("Which game?")).toBeNull();
  });

  it("comes first with several games; Minecraft goes on to today's steps", async () => {
    const user = setup(() => Promise.resolve([MINECRAFT, FACTORIO]));
    expect(await screen.findByText("Which game?")).toBeInTheDocument();
    expect(stepNames()).toEqual(["Game", ...TODAY_STEPS]);
    expect(screen.getByRole("radio", { name: /Factorio/ })).toBeEnabled();
    expect(screen.getByText("Java edition. 4 GB suits a small server.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByText("Choose a game")).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: /Minecraft/ }));
    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByText("What kind of server?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Back/ }));
    expect(await screen.findByText("Which game?")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Minecraft/ })).toBeChecked();
  });

  it("another game continues with the steps of its catalog entry", async () => {
    const user = setup(() => Promise.resolve([MINECRAFT, FACTORIO]));
    await user.click(await screen.findByRole("radio", { name: /Factorio/ }));
    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(stepNames()).toEqual(["Game", "Version", "Resources", "Settings", "Review"]);
    expect(await screen.findByRole("heading", { name: /Step 2 of 5: Version & options/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Back/ }));
    expect(await screen.findByText("Which game?")).toBeInTheDocument();
  });

  it("keeps Minecraft deep links on their step", async () => {
    setup(() => Promise.resolve([MINECRAFT, FACTORIO]), "/servers/new?mode=import");
    expect(await screen.findByRole("heading", { name: /Step 3 of 6/ })).toBeInTheDocument();
    expect(screen.queryByText("Which game?")).toBeNull();
  });
});
