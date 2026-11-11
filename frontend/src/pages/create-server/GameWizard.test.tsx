import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { AuthState, GameInfo } from "@/lib/types";
import { factorioGame, makeFactorioInstance } from "@/test/fixtures";
import CreateServer from "../CreateServer";

const MINECRAFT: GameInfo = {
  id: "minecraft",
  label: "Minecraft",
  variants: [{ id: "VANILLA", label: "Vanilla" }],
  capabilities: ["console_commands", "jvm"],
  memory_model: "jvm",
  default_memory_mb: 4096,
  notes: [],
};

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

let post: ReturnType<typeof vi.fn>;

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
  vi.spyOn(api, "get").mockImplementation((async (path: string) => {
    if (path === "/catalog/games") return [MINECRAFT, factorioGame()];
    if (path === "/catalog/games/factorio/versions")
      return {
        versions: [
          { id: "2.1.24", label: "2.1.24", stable: true, latest: true },
          { id: "2.1.23", label: "2.1.23", stable: true, latest: false },
        ],
        latest: "2.1.24",
      };
    if (path === "/nodes") return [];
    if (path === "/system/ports") return { range: [25565, 40000], in_use: [], suggested: 25565 };
    return new Promise(() => {});
  }) as never);
  post = vi.fn(async () => ({ instance: makeFactorioInstance(), job: { id: "j1" }, warnings: [], java: null }));
  vi.spyOn(api, "post").mockImplementation(post as never);
});
afterEach(() => vi.restoreAllMocks());

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(qk.auth, AUTH);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/servers/new"]}>
        <CreateServer />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("create wizard: Factorio", () => {
  it("walks through the catalog's steps and sends the body of contract B5", async () => {
    const user = setup();
    await user.click(await screen.findByRole("radio", { name: /Factorio/ }));
    await user.click(screen.getByRole("button", { name: /Next/ }));

    // version & options: the latest stable is preselected; Space Age is off by default and pinned
    expect(await screen.findByRole("heading", { name: /Version & options/ })).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Space Age" })).not.toBeChecked();
    expect(screen.getByText(/fixed once the server exists/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Next/ }));

    // resources & network: plain memory, the UDP game port
    expect(await screen.findByRole("heading", { name: /Resources & network/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Memory (MB)")).toHaveValue("2048");
    expect(screen.queryByText(/heap/i)).toBeNull();
    const port = screen.getByLabelText(/Game/);
    await user.type(port, "80");
    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByText("A port between 1024 and 65535, or empty for automatic")).toBeInTheDocument();
    await user.clear(port);
    await user.type(port, "34200");
    await user.type(screen.getByLabelText("Bind address"), "192.0.2.10");
    await user.click(screen.getByRole("button", { name: /Next/ }));

    // initial settings from the fields marked `initial`
    expect(await screen.findByRole("heading", { name: /Game settings/ })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Server name"), "My factory");
    await user.type(screen.getByLabelText(/Game password/), "pw");
    await user.type(screen.getByLabelText("Seed"), "123");
    await user.click(screen.getByRole("button", { name: "Review" }));

    expect(await screen.findByRole("heading", { name: /Review & deploy/ })).toBeInTheDocument();
    expect(screen.getByText("Game (UDP)")).toBeInTheDocument();
    expect(screen.getByText("34200")).toBeInTheDocument();
    // the bind address entered is part of the summary (real-stack run 2026-11-11, item 24)
    expect(screen.getByText("Bind address")).toBeInTheDocument();
    expect(screen.getByText("192.0.2.10")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Deploy server/ }));

    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0][0]).toBe("/instances/");
    expect(post.mock.calls[0][1]).toEqual({
      game: "factorio",
      name: "Factorio 2.1.24",
      version: "2.1.24",
      memory_mb: 2048,
      start: true,
      nightly_backup: true,
      options: { space_age: false },
      ports: { game: 34200 },
      host_ip: "192.0.2.10",
      settings: { "server-settings": { name: "My factory", game_password: "pw" }, "map-gen-settings": { seed: 123 } },
    });
  });
});
