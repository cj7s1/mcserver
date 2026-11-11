/**
 * The catalog shapes of the six Steam-era games (Palworld, Valheim, Project Zomboid, HKMP, CS2, Rust)
 * through the helpers that read them: labels/icons/colours, field and secret patterns, derived and
 * conditional ports, the create wizard's model, the Network editor's validation, the saves reset body
 * and the players lists. The entries are the backend's own (src/test/six-games.ts).
 */
import { Gamepad2 } from "lucide-react";
import { describe, expect, it } from "vitest";
import { buildGamePayload, initialGameState, validateGameStep, wizardConfigValues } from "@/pages/create-server/gameWizardModel";
import { resetBody } from "@/pages/instance/backups/saves-model";
import { validateNamedPorts } from "@/pages/instance/network/named-ports";
import { gamePlayerNameError, listEntries } from "@/pages/instance/players/game-players-model";
import { sixGame, SIX_GAME_IDS } from "@/test/six-games";
import { displayValue, fromText, patternError, patternRegExp } from "./game-config";
import { conditionText, derivedNumber, derivedPorts, derivedRule, editablePorts, followsOtherPort, portPublished } from "./game-ports";
import { gameColor, gameIcon, gameLabel, MINECRAFT, serverIconColor, updatesOnRestart, variantColor } from "./games";
import type { ConfigField, GameInfo, PlayersResponse } from "./types";

const field = (game: GameInfo, file: string, key: string): ConfigField => {
  const f = game.config_files?.find((c) => c.id === file)?.fields.find((x) => x.key === key);
  if (!f) throw new Error(`${game.id}: no field ${file}.${key}`);
  return f;
};

describe("labels, icons and colours of the six games", () => {
  it("names every game the way the catalog does", () => {
    for (const id of SIX_GAME_IDS) expect(gameLabel(id)).toBe(sixGame(id).label);
    expect(gameLabel({ game: "cs2", game_label: "" })).toBe("Counter-Strike 2");
  });

  it("gives each game its own icon and colour; unknown games keep the gamepad and grey", () => {
    const icons = new Set(SIX_GAME_IDS.map((id) => gameIcon(id)));
    expect(icons.size).toBe(6);
    expect(icons.has(Gamepad2)).toBe(false);
    expect(gameIcon(MINECRAFT)).not.toBe(Gamepad2);
    expect(gameIcon("some-new-game")).toBe(Gamepad2);
    expect(new Set(SIX_GAME_IDS.map((id) => gameColor(id))).size).toBe(6);
    expect(gameColor("some-new-game")).toBeUndefined();
  });

  it("server icons: the variant's colour first, then the game's, else grey (Minecraft and Terraria unchanged)", () => {
    expect(serverIconColor("PAPER", "minecraft")).toBe(variantColor("PAPER"));
    expect(serverIconColor("tshock", "terraria")).toBe(variantColor("tshock"));
    expect(serverIconColor("", "palworld")).toBe(gameColor("palworld"));
    expect(serverIconColor("", "factorio")).toBe(variantColor(""));
    expect(serverIconColor(undefined, undefined)).toBe(variantColor(undefined));
  });

  it("a branch updates at the next start; a fixed version number (HKMP) does not", () => {
    for (const id of ["palworld", "valheim", "project-zomboid", "cs2", "rust"] as const) expect(updatesOnRestart(sixGame(id).fixed_version)).toBe(true);
    expect(updatesOnRestart(sixGame("hkmp").fixed_version)).toBe(false);
    // the catalog says it (updates_on_restart); without the flag (Satisfactory) a restart updates the game
    expect(updatesOnRestart({})).toBe(true);
    expect(updatesOnRestart({ updates_on_restart: false })).toBe(false);
    expect(updatesOnRestart(undefined)).toBe(false);
  });
});

describe("patterns of fields and secrets", () => {
  const patterns = SIX_GAME_IDS.flatMap((id) => {
    const g = sixGame(id);
    return [
      ...(g.config_files ?? []).flatMap((f) => f.fields),
      ...(g.secrets ?? []),
      ...(g.world?.reset_fields ?? []),
    ]
      .filter((x) => x.pattern)
      .map((x) => [`${id}.${x.key}`, x.pattern!] as const);
  });

  it("every pattern the backend sends compiles in the browser", () => {
    expect(patterns.length).toBeGreaterThan(10);
    for (const [, p] of patterns) expect(patternRegExp(p)).toBeInstanceOf(RegExp);
    for (const id of SIX_GAME_IDS) expect(patternRegExp(sixGame(id).player_name_pattern!)).toBeInstanceOf(RegExp);
  });

  it("is matched whole, like Python's fullmatch (CS2's server name has two alternatives)", () => {
    const cs2Name = field(sixGame("cs2"), "server", "server_name");
    expect(fromText(cs2Name, "My CS2 server")).toEqual({ value: "My CS2 server" });
    expect(fromText(cs2Name, "x")).toEqual({ value: "x" });
    // shell metacharacters never reach CS2's start line
    expect(fromText(cs2Name, "a;rm -rf /")).toEqual({ error: `Allowed: ${cs2Name.pattern_help}` });
    expect(fromText(cs2Name, "trailing ")).toHaveProperty("error");
    const rustName = field(sixGame("rust"), "server", "server_name");
    expect(fromText(rustName, "+server.secure 0")).toHaveProperty("error");
    expect(fromText(rustName, 'say "hi"')).toHaveProperty("error");
    expect(fromText(rustName, "Rust ÄÖ 😀")).toEqual({ value: "Rust ÄÖ 😀" });
  });

  it("an empty nullable value is null and not checked; an empty required one is", () => {
    const reset = sixGame("valheim").world!.reset_fields!.find((f) => f.key === "world_name")!;
    expect(fromText(reset, "")).toEqual({ value: null });
    expect(fromText(reset, "New World")).toHaveProperty("error");
    expect(fromText(reset, "New_World-2")).toEqual({ value: "New_World-2" });
    const palName = field(sixGame("palworld"), "server", "server_name");
    expect(fromText(palName, "")).toHaveProperty("error");
    expect(fromText(field(sixGame("palworld"), "server", "description"), "")).toEqual({ value: "" });
  });

  it("secrets: generated passwords, GSLT and join passwords say what they allow", () => {
    const [admin, join] = sixGame("palworld").secrets!;
    expect(admin.generated && admin.reveal).toBe(true);
    expect(patternError("short", admin.pattern, admin.pattern_help)).toBe(`Allowed: ${admin.pattern_help}`);
    expect(patternError("a-good_admin_pw", admin.pattern, admin.pattern_help)).toBeNull();
    expect(patternError("pass word", join.pattern, join.pattern_help)).not.toBeNull();
    const gslt = sixGame("cs2").secrets!.find((s) => s.key === "gslt")!;
    expect(gslt.link).toMatch(/^https:\/\/steamcommunity\.com\//);
    expect(patternError("0123456789ABCDEF0123456789ABCDEF", gslt.pattern, gslt.pattern_help)).toBeNull();
    expect(patternError("0123456789abcdef0123456789abcdef", gslt.pattern, gslt.pattern_help)).not.toBeNull();
    expect(patternError("anything", undefined)).toBeNull();
    expect(patternError("anything", "(unclosed")).toBeNull(); // the server still checks it
  });

  it("enums with an empty option show it as the game's default; option labels are used", () => {
    const preset = field(sixGame("valheim"), "server", "preset");
    expect(displayValue(preset, "")).toBe("Default");
    expect(displayValue(preset, "hard")).toBe("hard");
    const branch = sixGame("project-zomboid").options!.find((o) => o.key === "branch")!;
    expect(displayValue(branch, "legacy41")).toBe("Build 41 (legacy41)");
  });
});

describe("derived, conditional and 1:1 ports", () => {
  it("Valheim: only the game port is chosen; query and crossplay follow it", () => {
    const ports = sixGame("valheim").ports!;
    expect(editablePorts(ports).map((p) => p.name)).toEqual(["game"]);
    expect(ports.filter(followsOtherPort).map((p) => p.name)).toEqual(["query", "crossplay"]);
    expect(derivedPorts(ports, "game").map((p) => p.name)).toEqual(["query", "crossplay"]);
    const [, query, crossplay] = ports;
    expect(derivedRule(ports, query)).toBe("Game port + 1");
    expect(derivedNumber(crossplay, 2500)).toBe(2502);
    expect(derivedNumber(crossplay, Number.NaN)).toBeNaN();
    expect(ports.every((p) => p.one_to_one)).toBe(true);
  });

  it("a conditional port names its setting and is published only while it is on", () => {
    const valheim = sixGame("valheim");
    const crossplay = valheim.ports![2];
    expect(conditionText(valheim, crossplay)).toBe("only while “Crossplay (Xbox and Microsoft Store players)” is on");
    expect(portPublished(valheim, crossplay, { server: { crossplay: true } })).toBe(true);
    expect(portPublished(valheim, crossplay, { server: { crossplay: false } })).toBe(false);
    expect(portPublished(valheim, crossplay, {})).toBe(false); // the field's default (off)
    expect(portPublished(valheim, valheim.ports![0], {})).toBe(true);
    const cs2 = sixGame("cs2");
    const tv = cs2.ports!.find((p) => p.name === "tv")!;
    expect(editablePorts(cs2.ports).map((p) => p.name)).toEqual(["game", "tv"]);
    expect(conditionText(cs2, tv)).toBe("only while “CSTV (spectators)” is on");
    const rust = sixGame("rust");
    expect(portPublished(rust, rust.ports!.find((p) => p.name === "app")!, { server: { rust_plus: true } })).toBe(true);
    expect(portPublished(undefined, tv, null)).toBeNull();
  });

  it("the Network editor checks the derived numbers too", () => {
    const ports = sixGame("valheim").ports!;
    const row = (host_port: string) => [{ name: "game", label: "Game", protocol: "udp" as const, container_port: 2456, host_port }];
    expect(validateNamedPorts(row("2456"), ports)).toEqual({});
    expect(validateNamedPorts(row("65534"), ports).game).toBe("Crossplay would be port 65536; choose a lower port");
    // without the catalog it checks what it did before
    expect(validateNamedPorts(row("65534"))).toEqual({});
  });
});

describe("create wizard model", () => {
  it("Valheim: the derived ports are never sent and must fit below 65536", () => {
    const game = sixGame("valheim");
    const state = { ...initialGameState(game), ports: { game: "65535", query: "", crossplay: "" } };
    expect(validateGameStep("resources", game, state, "public")["ports.game"]).toBe("Steam query would be port 65536; choose a lower port");
    const ok = { ...state, ports: { game: "2500", query: "", crossplay: "" } };
    expect(validateGameStep("resources", game, ok, "public")).toEqual({});
    expect(buildGamePayload(game, { ...ok, secrets: { server_password: "secret1" } }, "public", true).ports).toEqual({ game: 2500 });
  });

  it("whether a conditional port is published follows the wizard's settings", () => {
    const game = sixGame("valheim");
    const state = initialGameState(game);
    expect(wizardConfigValues(game, state).server.crossplay).toBe(false);
    const on = { ...state, settings: { ...state.settings, server: { ...state.settings.server, crossplay: "true" } } };
    expect(portPublished(game, game.ports![2], wizardConfigValues(game, on))).toBe(true);
  });

  it("secrets: generated ones are optional, required ones are asked, patterns are checked", () => {
    const pal = sixGame("palworld");
    const state = initialGameState(pal);
    expect(validateGameStep("review", pal, state, "public")).toEqual({});
    expect(validateGameStep("review", pal, { ...state, secrets: { admin_password: "short" } }, "public")["secrets.admin_password"]).toMatch(/^Allowed: 12 to 64/);
    const valheim = sixGame("valheim");
    expect(validateGameStep("review", valheim, initialGameState(valheim), "public")["secrets.server_password"]).toBe("Join password is required");
    expect(validateGameStep("review", valheim, { ...initialGameState(valheim), secrets: { server_password: "abcde" } }, "public")).toEqual({});
  });

  it("options and settings of every game start valid (defaults pass their own patterns)", () => {
    for (const id of SIX_GAME_IDS) {
      const game = sixGame(id);
      const state = initialGameState(game);
      expect(validateGameStep("software", game, state, game.fixed_version!.id)).toEqual({});
      expect(validateGameStep("settings", game, state, game.fixed_version!.id)).toEqual({});
      expect(validateGameStep("resources", game, state, game.fixed_version!.id)).toEqual({});
    }
  });

  it("Project Zomboid's branch is a pinned option with labels; the payload sends the choice", () => {
    const pz = sixGame("project-zomboid");
    const state = { ...initialGameState(pz), options: { server_name: "servertest", branch: "legacy41" } };
    expect(buildGamePayload(pz, state, "public", false).options).toEqual({ server_name: "servertest", branch: "legacy41" });
  });
});

describe("saves and players shapes", () => {
  it("Rust's reset: the seed on its own (bounded), wiping blueprints in the settings", () => {
    const fields = sixGame("rust").world!.reset_fields!;
    expect(resetBody(fields, { seed: "", wipe_blueprints: "true" }, "Rusty", true)).toEqual({
      body: { confirm_name: "Rusty", seed: null, backup: true, settings: { wipe_blueprints: true } },
      errors: {},
    });
    expect(resetBody(fields, { seed: "2147483648", wipe_blueprints: "false" }, "Rusty", true).errors.seed).toBe("Must be at most 2147483647");
    expect(resetBody(fields, { seed: "42", wipe_blueprints: "false" }, "Rusty", false).body).toEqual({ confirm_name: "Rusty", seed: 42, backup: false });
  });

  it("Palworld's reset has no fields; Valheim's new world name is checked", () => {
    expect(sixGame("palworld").world!.reset_fields).toEqual([]);
    expect(resetBody([], {}, "Pals", true).body).toEqual({ confirm_name: "Pals", seed: null, backup: true });
    const fields = sixGame("valheim").world!.reset_fields!;
    expect(resetBody(fields, { world_name: "bad name" }, "V", true).errors.world_name).toMatch(/^Allowed:/);
    expect(resetBody(fields, { world_name: "Midgard" }, "V", true).body).toEqual({ confirm_name: "V", seed: null, backup: true, settings: { world_name: "Midgard" } });
  });

  it("player names: SteamID64s for Valheim, names with spaces for Rust, IPs for HKMP", () => {
    expect(gamePlayerNameError("76561198000000000", sixGame("valheim").player_name_pattern)).toBeNull();
    expect(gamePlayerNameError("Viking", sixGame("valheim").player_name_pattern)).not.toBeNull();
    expect(gamePlayerNameError("Some Player", sixGame("rust").player_name_pattern)).toBeNull();
    expect(gamePlayerNameError('a"b', sixGame("rust").player_name_pattern)).not.toBeNull();
    expect(gamePlayerNameError("192.0.2.7", sixGame("hkmp").player_name_pattern)).toBeNull();
  });

  it("list entries: Rust's ids with names, HKMP's IP bans after the name bans", () => {
    const rust: Pick<PlayersResponse, "whitelist" | "ops" | "bans" | "ip_bans"> = {
      whitelist: [],
      ops: [{ name: "76561198000000001", display_name: "Owner" }],
      bans: [{ name: "76561198000000002", display_name: "", reason: "cheating" }],
      ip_bans: [],
    };
    expect(listEntries(rust, "ops")).toEqual([{ name: "76561198000000001", display_name: "Owner", reason: undefined }]);
    expect(listEntries(rust, "bans")).toEqual([{ name: "76561198000000002", display_name: undefined, reason: "cheating" }]);
    const hkmp = { whitelist: [], ops: [], bans: [{ name: "authkey1", reason: "" }], ip_bans: [{ name: "", ip: "192.0.2.7", reason: "" }] };
    expect(listEntries(hkmp, "bans")).toEqual([
      { name: "authkey1", display_name: undefined, reason: "" },
      { name: "192.0.2.7", reason: "", ip: true },
    ]);
  });
});
