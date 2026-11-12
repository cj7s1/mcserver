/**
 * Suspicion of slice C (security review 2026-11-11): the player list files (whitelist.json, ops.json,
 * banned-players.json, usercache.json) are written by the game server; an entry whose `uuid` (or name) is
 * no text made `avatarUrl` throw and the whole Players page show its error screen. Such values are made
 * text or dropped when the answer arrives, and `avatarUrl` itself accepts any value.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, renderHook, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { OnlinePlayers } from "./OnlinePlayers";
import { PlayerLists } from "./PlayerLists";
import { avatarUrl, normalizePlayers, usePlayers, type McPlayers } from "./player-utils";

const ID = "12121212-1212-1212-1212-121212121212";

/** What a server could write into its list files (and the panel passes on as it is). */
const HOSTILE = {
  running: true,
  whitelist: [{ uuid: 12345, name: "Alex" }, "not an object", null, { uuid: { a: 1 }, name: ["x"] }],
  ops: [{ uuid: "069a79f4-44e9-4726-a5be-fca90e38aaf5", name: "Notch", level: "4", bypassesPlayerLimit: "yes" }],
  bans: [{ uuid: true, name: 42, reason: { text: "x" }, created: 1, expires: "forever" }],
  ip_bans: [{ ip: 3232235777, reason: null }],
  known_players: [{ name: "Alex", uuid: 7 }, { name: null, uuid: "u" }, 5],
  online: { online: 1, max: 20, players: ["Alex"] },
} as unknown as McPlayers;

afterEach(() => vi.restoreAllMocks());

describe("player list files written by the server", () => {
  it("avatarUrl ignores values that are no text", () => {
    expect(avatarUrl({ uuid: 12345 as unknown as string, name: "Alex" })).toBe("https://mc-heads.net/avatar/Alex/32");
    expect(avatarUrl({ uuid: { a: 1 } as unknown as string })).toBeNull();
    expect(avatarUrl({ uuid: "069a79f4-44e9-4726-a5be-fca90e38aaf5", name: "Notch" })).toBe(
      "https://mc-heads.net/avatar/069a79f444e94726a5befca90e38aaf5/32",
    );
    expect(avatarUrl({ name: "Notch" })).toBe("https://mc-heads.net/avatar/Notch/32");
    expect(avatarUrl({})).toBeNull();
  });

  it("normalizePlayers keeps text, turns numbers into text and drops the rest", () => {
    const n = normalizePlayers(HOSTILE);
    expect(n.whitelist).toEqual([{ uuid: "12345", name: "Alex" }, {}]);
    expect(n.ops).toEqual([{ uuid: "069a79f4-44e9-4726-a5be-fca90e38aaf5", name: "Notch" }]);
    expect(n.bans).toEqual([{ name: "42", created: "1", expires: "forever" }]);
    expect(n.ip_bans).toEqual([{ ip: "3232235777" }]);
    expect(n.known_players).toEqual([{ name: "Alex", uuid: "7" }]);
    expect(n.online).toBe(HOSTILE.online);
    // a well-formed answer is unchanged
    const good: McPlayers = { running: false, whitelist: [{ uuid: "u-1", name: "A" }], ops: [], bans: [], ip_bans: [], known_players: [{ name: "A", uuid: "u-1" }] };
    expect(normalizePlayers(good)).toEqual(good);
  });

  it("the page renders such an answer with player heads on", async () => {
    vi.spyOn(api, "get").mockResolvedValue(structuredClone(HOSTILE) as never);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    );
    const { result } = renderHook(() => usePlayers(ID, false), { wrapper });
    await waitFor(() => expect(result.current.data).toBeDefined());
    const data = result.current.data!;
    render(
      <>
        <OnlinePlayers instanceId={ID} data={data} canManage showHeads />
        <PlayerLists instanceId={ID} data={data} canManage showHeads />
      </>,
      { wrapper },
    );
    expect(screen.getByRole("tab", { name: /Whitelist/ })).toBeInTheDocument();
    expect(screen.getAllByText("Alex").length).toBeGreaterThan(0);
  });
});
