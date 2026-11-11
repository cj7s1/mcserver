import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { AuthState, GameInfo } from "@/lib/types";
import { makeFactorioInstance } from "@/test/fixtures";
import CreateServer from "../CreateServer";
import { buildGamePayload, initialGameState, validateGameStep, variantView, wizardVariants } from "./gameWizardModel";

/** Terraria's catalog entry as the panel sends it (apps/games/terraria.py), shortened. */
function terrariaGame(): GameInfo {
  return {
    id: "terraria",
    label: "Terraria",
    variants: [
      { id: "vanilla", label: "Terraria (vanilla)", default_memory_mb: 1536, notes: ["The official dedicated server."], game_version: null },
      {
        id: "tmodloader",
        label: "tModLoader",
        default_memory_mb: 3072,
        notes: ["The first start of every new container downloads the .NET runtime."],
        game_version: "1.4.4.9",
      },
      { id: "tshock", label: "TShock", default_memory_mb: 1536, notes: ["The panel creates the owner account."], game_version: null },
    ],
    capabilities: ["console_commands", "players", "query", "config_files", "world_reset", "world_import_export", "software_update"],
    memory_model: "plain",
    default_memory_mb: 1536,
    notes: ["Three flavours."],
    ports: [{ name: "game", label: "Game", protocol: "tcp", container_port: 7777, default_host_port: 7777 }],
    options: [
      { key: "world_name", type: "string", label: "World name", default: "world", pinned: true },
      { key: "owner_account", type: "string", label: "TShock owner account", default: "owner", pinned: true, variants: ["tshock"] },
    ],
    config_files: [
      {
        id: "serverconfig",
        path: "serverconfig.txt",
        codec: "kv",
        label: "Server config",
        variants: ["vanilla"],
        fields: [
          { key: "autocreate", type: "enum", label: "World size", options: ["1", "2", "3"], default: "2", initial: true },
          { key: "maxplayers", type: "int", label: "Max players", default: 8, min: 1, max: 255, initial: true },
        ],
      },
      {
        id: "serverconfig",
        path: "serverconfig.txt",
        codec: "kv",
        label: "Server config",
        variants: ["tmodloader"],
        fields: [{ key: "autocreate", type: "enum", label: "World size", options: ["1", "2", "3"], default: "2", initial: true }],
      },
      {
        id: "serverconfig",
        path: "serverconfig.txt",
        codec: "kv",
        label: "Server config",
        variants: ["tshock"],
        fields: [{ key: "autocreate", type: "enum", label: "World size", options: ["1", "2", "3"], default: "2", initial: true }],
      },
      {
        id: "tshock",
        path: "config.json",
        codec: "json",
        label: "TShock",
        variants: ["tshock"],
        fields: [{ key: "Settings.MaxSlots", type: "int", label: "Max slots", default: 8, initial: true }],
      },
    ],
    secrets: [{ key: "owner_password", label: "TShock owner password", required: false, variants: ["tshock"] }],
    setup_steps: [{ id: "owner_account", label: "Create the TShock owner account", kind: "secret", secret: "owner_password" }],
    stop_words: ["exit", "exit-nosave", "off", "off-nosave"],
    has_versions: true,
    min_memory_mb: 1024,
    world: { kind: "worlds", extension: ".wld", reset_fields: [] },
  };
}

const MINECRAFT: GameInfo = {
  id: "minecraft",
  label: "Minecraft",
  variants: [{ id: "VANILLA", label: "Vanilla" }],
  capabilities: ["console_commands", "jvm"],
  memory_model: "jvm",
  default_memory_mb: 4096,
  notes: [],
};

describe("Terraria wizard model", () => {
  it("offers the catalog's variants, not Minecraft's server types", () => {
    expect(wizardVariants(terrariaGame()).map((v) => v.id)).toEqual(["vanilla", "tmodloader", "tshock"]);
    expect(wizardVariants(MINECRAFT)).toEqual([]);
    expect(variantView(MINECRAFT, null)).toBe(MINECRAFT);
  });

  it("narrows options, files, secrets and setup steps to the variant", () => {
    const vanilla = variantView(terrariaGame(), "vanilla");
    expect(vanilla.options?.map((o) => o.key)).toEqual(["world_name"]);
    expect(vanilla.config_files?.map((f) => `${f.id}:${f.fields.length}`)).toEqual(["serverconfig:2"]);
    expect(vanilla.secrets).toEqual([]);
    expect(vanilla.setup_steps).toEqual([]);
    const tshock = variantView(terrariaGame(), "tshock");
    expect(tshock.options?.map((o) => o.key)).toEqual(["world_name", "owner_account"]);
    expect(tshock.config_files?.map((f) => f.id)).toEqual(["serverconfig", "tshock"]);
    expect(tshock.secrets?.map((s) => s.key)).toEqual(["owner_password"]);
    expect(tshock.setup_steps?.map((s) => s.id)).toEqual(["owner_account"]);
    expect(variantView(terrariaGame(), "tmodloader").default_memory_mb).toBe(3072);
  });

  it("sends the variant and only the variant's choices", () => {
    const game = terrariaGame();
    const state = { ...initialGameState(game, "tshock"), secrets: { owner_password: "Pw-1234" } };
    expect(state.variant).toBe("tshock");
    const view = variantView(game, "tshock");
    const payload = buildGamePayload(view, state, "1.4.5.8-6.2.1", true);
    expect(payload).toMatchObject({ game: "terraria", variant: "tshock", version: "1.4.5.8-6.2.1", memory_mb: 1536 });
    expect(payload.options).toEqual({ world_name: "world", owner_account: "owner" });
    expect(payload.secrets).toEqual({ owner_password: "Pw-1234" });
    const vanilla = buildGamePayload(variantView(game, "vanilla"), initialGameState(game), "1.4.5.8", true);
    expect(vanilla.variant).toBe("vanilla");
    expect(vanilla.options).toEqual({ world_name: "world" });
    expect(vanilla.secrets).toBeUndefined();
    expect(validateGameStep("software", game, { ...initialGameState(game), variant: null }, "1.4.5.8").variant).toBe("Choose a variant");
  });
});

// --- the wizard on screen ------------------------------------------------------------------------------------
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
let versionQueries: unknown[];

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
  versionQueries = [];
  vi.spyOn(api, "get").mockImplementation((async (path: string, query?: { variant?: string }) => {
    if (path === "/catalog/games") return [MINECRAFT, terrariaGame()];
    if (path === "/catalog/games/terraria/versions") {
      versionQueries.push(query);
      if (query?.variant === "tmodloader")
        return {
          versions: [
            { id: "2026.10.2.3", label: "2026.10.2.3 (Terraria 1.4.4.9)", stable: false, latest: false },
            { id: "2026.09.3.1", label: "2026.09.3.1 (Terraria 1.4.4.9)", stable: true, latest: true },
          ],
          latest: "2026.09.3.1",
        };
      return { versions: [{ id: "1.4.5.8", label: "1.4.5.8", stable: true, latest: true }], latest: "1.4.5.8" };
    }
    if (path === "/nodes") return [];
    if (path === "/system/ports") return { range: [25565, 40000], in_use: [], suggested: 25565 };
    return new Promise(() => {});
  }) as never);
  post = vi.fn(async () => ({ instance: makeFactorioInstance({ game: "terraria" }), job: { id: "j1" }, warnings: [], java: null }));
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

describe("create wizard: Terraria", () => {
  it("picks tModLoader, warns about its Terraria version and sends the variant", async () => {
    const user = setup();
    await user.click(await screen.findByRole("radio", { name: /Terraria/ }));
    await user.click(screen.getByRole("button", { name: /Next/ }));

    expect(await screen.findByRole("heading", { name: /Version & options/ })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /Variant/ })).toHaveTextContent("Terraria (vanilla)");
    expect(screen.getByLabelText("World name")).toHaveValue("world");
    expect(screen.queryByLabelText("TShock owner account")).toBeNull();
    await user.click(screen.getByRole("combobox", { name: /Variant/ }));
    await user.click(await screen.findByRole("option", { name: /tModLoader/ }));
    expect(await screen.findByRole("note")).toHaveTextContent("vanilla clients on newer Terraria versions cannot join");
    expect(within(screen.getByRole("list", { name: /About tModLoader/ })).getByText(/downloads the .NET runtime/)).toBeInTheDocument();
    expect(versionQueries).toContainEqual({ variant: "tmodloader" });
    expect(await screen.findByRole("combobox", { name: /^Version/ })).toHaveTextContent("2026.09.3.1");
    await user.click(screen.getByRole("button", { name: /Next/ }));

    expect(await screen.findByRole("heading", { name: /Resources & network/ })).toBeInTheDocument();
    expect(screen.getByLabelText("Memory (MB)")).toHaveValue("3072");
    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByRole("heading", { name: /Game settings/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("Max players")).toBeNull(); // the vanilla-only field is not asked
    await user.click(screen.getByRole("button", { name: "Review" }));

    expect(await screen.findByRole("heading", { name: /Review & deploy/ })).toBeInTheDocument();
    expect(screen.getByText("Variant")).toBeInTheDocument();
    expect(screen.queryByText("TShock owner password")).toBeNull();
    await user.click(screen.getByRole("button", { name: /Deploy server/ }));
    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0][1]).toEqual({
      game: "terraria",
      variant: "tmodloader",
      name: "Terraria 2026.09.3.1",
      version: "2026.09.3.1",
      memory_mb: 3072,
      start: true,
      nightly_backup: true,
      options: { world_name: "world" },
    });
  });
});
