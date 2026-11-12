/**
 * C-5 (security review 2026-11-11, I-4): a player action by a typed display name that Rust or Palworld
 * cannot resolve without guessing answers 409 `ambiguous_player` with the candidates; the page lets the
 * user pick one and sends the same action again with its id. Nothing is sent without a choice.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Instance, PlayersResponse } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { sixGame, SIX_GAME_IDS } from "@/test/six-games";
import { GamePlayers } from "./GamePlayers";
import { ambiguousCandidates } from "./player-utils";

const RUST: Instance = makeInstance({
  id: "77777777-7777-7777-7777-777777777777",
  name: "Rust test",
  game: "rust",
  game_label: "Rust",
  capabilities: sixGame("rust").capabilities,
  memory_model: "plain",
  status: "running",
  permissions: ["instance.view", "instance.players"],
});

const TWO = [
  { name: "Chief", id: "76561198000000001" },
  { name: "chief", id: "76561198000000002" },
];

const players = (over: Partial<PlayersResponse> = {}): PlayersResponse => ({
  running: true,
  whitelist: [],
  ops: [],
  bans: [],
  ip_bans: [],
  known_players: [],
  labels: { ops: "Owners (admins)", bans: "Bans" },
  online: { online: 2, max: 50, players: ["Chief", "chief"], entries: TWO },
  ...over,
});

let post: ReturnType<typeof vi.fn>;

beforeEach(() => {
  const gets: Record<string, unknown> = {
    "/catalog/games": SIX_GAME_IDS.map(sixGame),
    [`/instances/${RUST.id}/players`]: players(),
  };
  vi.spyOn(api, "get").mockImplementation(((path: string) =>
    path in gets ? Promise.resolve(structuredClone(gets[path])) : new Promise(() => {})) as never);
  post = vi.fn();
  vi.spyOn(api, "post").mockImplementation(post as never);
});
afterEach(() => vi.restoreAllMocks());

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(qk.auth, { setup_required: false, authenticated: true, user: null });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <GamePlayers instance={RUST} canManage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("ambiguous_player", () => {
  it("reads only well-formed candidates of that code", () => {
    const err = (details: Record<string, unknown>, code = "ambiguous_player", status = 409) => new ApiError(status, code, "?", details);
    expect(ambiguousCandidates(err({ candidates: TWO }))).toEqual(TWO);
    expect(ambiguousCandidates(err({ candidates: [{ name: "x", id: 5 }, null, "y", { name: "Ok", id: "1" }] }))).toEqual([{ name: "Ok", id: "1" }]);
    expect(ambiguousCandidates(err({ candidates: [] }))).toBeNull();
    expect(ambiguousCandidates(err({}))).toBeNull();
    expect(ambiguousCandidates(err({ candidates: TWO }, "invalid_player_name", 400))).toBeNull();
    expect(ambiguousCandidates(new Error("ambiguous_player"))).toBeNull();
  });

  it("two online players with the name: pick one, the ban is sent again with its id and the form clears", async () => {
    post
      .mockRejectedValueOnce(new ApiError(409, "ambiguous_player", "More than one online player is called 'chief': choose one by its id", { candidates: TWO }))
      .mockResolvedValueOnce({ via: "api", output: "Banned" });
    const user = renderPage();
    const bans = (await screen.findByRole("heading", { name: "Bans" })).closest("section")!;
    const input = within(bans).getByRole("textbox", { name: "Player name to add to Bans" });
    await user.type(input, "chief");
    await user.type(within(bans).getByLabelText("Reason (optional)"), "griefing");
    await user.click(within(bans).getByRole("button", { name: "Ban" }));

    const dialog = await screen.findByRole("alertdialog", { name: "Which “chief” do you mean?" });
    expect(within(dialog).getByText(/More than one online player is called 'chief'/)).toBeInTheDocument();
    expect(within(dialog).getByText("76561198000000001")).toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1);
    await user.click(within(dialog).getByRole("button", { name: "Ban chief (76561198000000002)" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post).toHaveBeenLastCalledWith(`/instances/${RUST.id}/players`, { action: "ban", name: "76561198000000002", reason: "griefing" });
    await waitFor(() => expect(input).toHaveValue(""));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("a display name to confirm (one candidate) and Cancel: nothing more is sent, the name stays", async () => {
    post.mockRejectedValueOnce(
      new ApiError(409, "ambiguous_player", "'Chief' is a display name, which any player can take: confirm the player by its id (76561198000000001)", {
        candidates: [TWO[0]],
      }),
    );
    const user = renderPage();
    const owners = (await screen.findByRole("heading", { name: "Owners (admins)" })).closest("section")!;
    const input = within(owners).getByRole("textbox", { name: "Player name to add to Owners (admins)" });
    await user.type(input, "Chief");
    await user.click(within(owners).getByRole("button", { name: "Promote" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Confirm the player by its id" });
    expect(within(dialog).getByRole("button", { name: "Promote Chief (76561198000000001)" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(post).toHaveBeenCalledTimes(1);
    expect(input).toHaveValue("Chief");
  });

  it("other errors are shown as before (no choice)", async () => {
    post.mockRejectedValueOnce(new ApiError(400, "invalid_player_name", "Use the player's SteamID64, or the name of a player who is online"));
    const user = renderPage();
    const bans = (await screen.findByRole("heading", { name: "Bans" })).closest("section")!;
    await user.type(within(bans).getByRole("textbox", { name: "Player name to add to Bans" }), "Nobody");
    await user.click(within(bans).getByRole("button", { name: "Ban" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});
