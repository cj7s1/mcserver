/**
 * Satisfactory in the UI: the create wizard (no version to pick, the explicit join choice with the
 * join password required only for "password", the API port that is never asked for), the count-only
 * players page, the network editor without the API port, the setup card's "no port published yet",
 * "Update now" (a restart) and the saves section of a game whose saves go through its API.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { displayValue } from "@/lib/game-config";
import { gameLabel } from "@/lib/games";
import { qk } from "@/lib/queries";
import type { AuthState, GameInfo, Instance, PermissionCode } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import CreateServer from "./CreateServer";
import { secretRequiredNow } from "./create-server/gameWizardModel";
import { sharedPortsLabel } from "./create-server/shared-ports";
import { SavesSection } from "./instance/backups/SavesSection";
import { GameNetwork } from "./instance/network/GameNetwork";
import { BranchVersion } from "./instance/overview/BranchVersion";
import { SetupCard } from "./instance/overview/SetupCard";
import { PlayerCount } from "./instance/players/PlayerCount";

function satisfactoryGame(): GameInfo {
  return {
    id: "satisfactory",
    label: "Satisfactory",
    variants: [],
    capabilities: ["config_files", "consistent_backups", "players", "query", "world_import_export", "world_reset"],
    memory_model: "plain",
    default_memory_mb: 12288,
    min_memory_mb: 8192,
    notes: ["Needs at least 8 GB of memory."],
    fixed_version: { id: "stable", label: "Steam stable branch", help: "Checked at every start." },
    ports: [
      { name: "game", label: "Game", protocol: "udp", container_port: 7777, default_host_port: 7777 },
      { name: "api", label: "Server API", protocol: "tcp", container_port: 7777, default_host_port: 7777, same_port_as: "game" },
      { name: "messaging", label: "Reliable messaging", protocol: "tcp", container_port: 8888, default_host_port: 8888 },
    ],
    options: [
      {
        key: "access",
        type: "enum",
        label: "Who may join",
        default: null,
        pinned: false,
        options: ["password", "open"],
        option_labels: { password: "A join password", open: "Open to anyone who knows the address" },
      },
    ],
    config_files: [
      {
        id: "launch",
        path: "(container settings)",
        codec: "json",
        label: "Launch settings",
        fields: [{ key: "max_players", type: "int", label: "Max players", default: 4, min: 1, max: 127, initial: true }],
      },
    ],
    secrets: [
      { key: "client_password", label: "Join password (client password)", required: false, required_when: { option: "access", value: "password" } },
    ],
    setup_steps: [
      { id: "claim", label: "Claim the server", kind: "auto" },
      { id: "access", label: "Join password", kind: "auto" },
      { id: "new_game", label: "Create the game", kind: "auto" },
      { id: "publish", label: "Publish the ports", kind: "auto" },
    ],
    world: { kind: "saves", extension: ".sav", via_api: true, reset_fields: [{ key: "session_name", type: "string", label: "Session name", nullable: true }] },
  };
}

const PERMS: PermissionCode[] = ["instance.view", "instance.control", "instance.configure", "instance.restore", "instance.files.read", "instance.files.write"];

function satisfactoryInstance(over: Partial<Instance> = {}): Instance {
  return makeInstance({
    id: "33333333-3333-3333-3333-333333333333",
    name: "Ficsit",
    server_type: "" as Instance["server_type"],
    minecraft_version: "stable",
    image: "wolveix/satisfactory-server:v1.9.11",
    memory_mb: 12288,
    container_memory_mb: 12288,
    game: "satisfactory",
    game_label: "Satisfactory",
    capabilities: satisfactoryGame().capabilities,
    memory_model: "plain",
    permissions: PERMS,
    ports: [
      { name: "game", label: "Game", protocol: "udp", container_port: 7777, host_port: 7777, host_ip: "0.0.0.0" },
      { name: "api", label: "Server API", protocol: "tcp", container_port: 7777, host_port: 7777, host_ip: "0.0.0.0" },
      { name: "messaging", label: "Reliable messaging", protocol: "tcp", container_port: 8888, host_port: 8888, host_ip: "0.0.0.0" },
    ],
    game_options: { access: "open", session_name: "Ficsit 20261110" },
    setup: null,
    ...over,
  });
}

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

let gets: Record<string, unknown>;
let post: ReturnType<typeof vi.fn>;

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Element.prototype.scrollIntoView ??= function scrollIntoView() {};
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.releasePointerCapture ??= () => {};
  window.scrollTo = (() => {}) as typeof window.scrollTo;
});

beforeEach(() => {
  gets = {
    "/catalog/games": [
      { id: "minecraft", label: "Minecraft", variants: [], capabilities: ["jvm"], memory_model: "jvm", default_memory_mb: 4096, notes: [] },
      satisfactoryGame(),
    ],
    "/nodes": [],
    "/system/ports": { range: [25565, 40000], in_use: [], suggested: 25565 },
  };
  vi.spyOn(api, "get").mockImplementation(((path: string) =>
    path in gets ? Promise.resolve(structuredClone(gets[path])) : new Promise(() => {})) as never);
  post = vi.fn(async () => ({ instance: satisfactoryInstance(), job: { id: "j1" }, warnings: [], java: null }));
  vi.spyOn(api, "post").mockImplementation(post as never);
});
afterEach(() => vi.restoreAllMocks());

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(qk.auth, AUTH);
  render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/servers/new"]}>{children}</MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("create wizard: Satisfactory", () => {
  it("asks for no version, makes the join choice explicit and never asks for the API port", async () => {
    const user = wrap(<CreateServer />);
    await user.click(await screen.findByRole("radio", { name: /Satisfactory/ }));
    await user.click(screen.getByRole("button", { name: /Next/ }));

    // no version select: the stable branch, explained
    expect(await screen.findByText("Steam stable branch")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Version" })).toBeNull();
    // the join choice has no default
    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByText(/Choose one of password, open/)).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: /Who may join/ }));
    await user.click(await screen.findByRole("option", { name: "A join password" }));
    await user.click(screen.getByRole("button", { name: /Next/ }));

    // resources & network: 12 GB by default, the game port is UDP + TCP, no API port input
    expect(await screen.findByRole("heading", { name: /Resources & network/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Memory (MB)")).toHaveValue("12288");
    expect(screen.getByText("udp + tcp")).toBeInTheDocument();
    expect(screen.queryByLabelText(/Server API/)).toBeNull();
    const memory = screen.getByLabelText("Memory (MB)");
    await user.clear(memory);
    await user.type(memory, "4096");
    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByText("Memory must be at least 8192 MB")).toBeInTheDocument();
    await user.clear(memory);
    await user.type(memory, "16384");
    await user.click(screen.getByRole("button", { name: /Next/ }));

    expect(await screen.findByRole("heading", { name: /Game settings/ })).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Max players"));
    await user.type(screen.getByLabelText("Max players"), "8");
    await user.click(screen.getByRole("button", { name: "Review" }));

    // review: the join password is required because "password" was chosen
    expect(await screen.findByRole("heading", { name: /Review & deploy/ })).toBeInTheDocument();
    expect(screen.getByText("Game (UDP + TCP)")).toBeInTheDocument();
    expect(screen.getByText("A join password")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Deploy server/ }));
    expect(await screen.findByText(/Join password \(client password\) is required/)).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText(/Join password/), "join-me");
    await user.click(screen.getByRole("button", { name: /Deploy server/ }));

    expect(post).toHaveBeenCalledTimes(1);
    const body = post.mock.calls[0][1] as Record<string, unknown>;
    expect(body).toMatchObject({
      game: "satisfactory",
      version: "stable",
      memory_mb: 16384,
      options: { access: "password" },
      secrets: { client_password: "join-me" },
      settings: { launch: { max_players: 8 } },
    });
    expect(body.ports).toBeUndefined();
    expect(api.get).not.toHaveBeenCalledWith("/catalog/games/satisfactory/versions", expect.anything());
  });

  it("knows when a secret is required", () => {
    const secret = satisfactoryGame().secrets![0];
    expect(secretRequiredNow(secret, { options: { access: "password" } })).toBe(true);
    expect(secretRequiredNow(secret, { options: { access: "open" } })).toBe(false);
    expect(secretRequiredNow({}, { options: {} })).toBe(false);
    const ports = satisfactoryGame().ports!;
    expect(sharedPortsLabel(ports, ports[0])).toBe("udp + tcp");
    expect(sharedPortsLabel(ports, ports[2])).toBe("tcp");
    expect(displayValue(satisfactoryGame().options![0], "open")).toBe("Open to anyone who knows the address");
    expect(gameLabel("satisfactory")).toBe("Satisfactory");
  });
});

describe("instance pages of a Satisfactory server", () => {
  it("shows the player count only", async () => {
    gets[`/instances/${satisfactoryInstance().id}/players`] = {
      running: true,
      count_only: true,
      whitelist: [],
      ops: [],
      bans: [],
      ip_bans: [],
      known_players: [],
      online: { online: 3, max: 8, players: [] },
    };
    wrap(<PlayerCount instance={satisfactoryInstance({ status: "running" })} />);
    expect(await screen.findByLabelText("Players online")).toHaveTextContent("3of 8");
    expect(screen.getByText(/not who: there are no names/)).toBeInTheDocument();
    expect(screen.queryByText("Whitelist")).toBeNull();
  });

  it("edits the game and messaging ports, never the API port", async () => {
    const patch = vi.spyOn(api, "patch").mockResolvedValue(satisfactoryInstance() as never);
    const user = wrap(<GameNetwork instance={satisfactoryInstance()} canEdit />);
    await screen.findByLabelText(/Game host port/);
    await waitFor(() => expect(screen.queryByLabelText(/Server API host port/)).toBeNull());
    const game = screen.getByLabelText(/Game host port/);
    await user.clear(game);
    await user.type(game, "29010");
    await user.click(screen.getByRole("button", { name: /Save/ }));
    expect(patch).toHaveBeenCalledWith(`/instances/${satisfactoryInstance().id}`, {
      ports: [
        { name: "game", host_port: 29010 },
        { name: "messaging", host_port: 8888 },
      ],
    });
  });

  it("says that no port is published before the setup is done", async () => {
    const setup = {
      needed: true,
      next: "claim",
      ports_open: false,
      steps: [{ id: "claim", label: "Claim the server", help: "", kind: "auto", status: "pending", message: "", secret: null }],
    };
    wrap(<SetupCard instance={satisfactoryInstance({ setup } as Partial<Instance>)} canConfigure />);
    expect(await screen.findByText(/No port is published until these steps are done/)).toBeInTheDocument();
  });

  it("updates by restarting", async () => {
    const user = wrap(
      <BranchVersion instance={satisfactoryInstance({ status: "running" })} info={satisfactoryGame().fixed_version!} canControl />,
    );
    await user.click(screen.getByRole("button", { name: /Update now/ }));
    expect(post).toHaveBeenCalledWith(`/instances/${satisfactoryInstance().id}/power`, { action: "restart" });
  });

  it("offers a new game and shows the sessions", async () => {
    const inst = satisfactoryInstance();
    gets[`/instances/${inst.id}/saves`] = {
      current: "Ficsit 20261110",
      saves: [{ name: "Ficsit_20261110_mcpanel", path: "saved/server/Ficsit_20261110_mcpanel.sav", size: 1000, modified: 1, current: true, session: "Ficsit 20261110" }],
      set_aside: [],
      options: {},
      reset_fields: satisfactoryGame().world!.reset_fields,
    };
    wrap(
      <SavesSection instance={inst} canChange canDownload tracked={{ job: null, track: () => {} } as never} showTracked={false} />,
    );
    expect(await screen.findByRole("button", { name: /New game/ })).toBeInTheDocument();
    expect(screen.getByText("Ficsit 20261110")).toBeInTheDocument();
    expect(screen.getByText(/go through the running server/)).toBeInTheDocument();
  });
});
