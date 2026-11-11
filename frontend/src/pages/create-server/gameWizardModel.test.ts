import { describe, expect, it } from "vitest";
import { factorioGame } from "@/test/fixtures";
import {
  buildGamePayload,
  effectiveVersion,
  gameStepForField,
  initialFields,
  initialGameState,
  validateGameStep,
} from "./gameWizardModel";

const game = factorioGame();
const CATALOG = {
  versions: [
    { id: "2.1.24", label: "2.1.24", stable: true, latest: true },
    { id: "2.1.23", label: "2.1.23", stable: true, latest: false },
  ],
  latest: "2.1.24",
};

describe("game wizard model", () => {
  it("defaults: latest stable version, Space Age off, automatic port, the game's memory", () => {
    const state = initialGameState(game);
    const version = effectiveVersion(state, CATALOG);
    expect(version).toBe("2.1.24");
    expect(buildGamePayload(game, state, version, true)).toEqual({
      game: "factorio",
      name: "Factorio 2.1.24",
      version: "2.1.24",
      memory_mb: 2048,
      start: true,
      nightly_backup: true,
      options: { space_age: false },
    });
  });

  it("asks only for the fields marked initial", () => {
    expect(initialFields(game).map((f) => [f.file, f.fields.map((x) => x.key)])).toEqual([
      ["server-settings", ["name", "visibility.public", "game_password", "max_players"]],
      ["map-gen-settings", ["seed"]],
    ]);
  });

  it("builds the body of contract B5 from everything chosen", () => {
    const state = {
      ...initialGameState(game),
      version: "2.1.23",
      options: { space_age: "true" },
      name: "  Factory ",
      memory: "4096",
      ports: { game: "34200" },
      hostIp: "192.0.2.5",
      settings: {
        "server-settings": { name: "My factory", "visibility.public": "false", game_password: "pw", max_players: "8" },
        "map-gen-settings": { seed: "123" },
      },
      description: "Weekend game",
      nightlyBackup: true,
      node: 2,
    };
    expect(buildGamePayload(game, state, effectiveVersion(state, CATALOG), false)).toEqual({
      game: "factorio",
      name: "Factory",
      version: "2.1.23",
      memory_mb: 4096,
      start: true,
      nightly_backup: false,
      options: { space_age: true },
      ports: { game: 34200 },
      settings: { "server-settings": { name: "My factory", game_password: "pw", max_players: 8 }, "map-gen-settings": { seed: 123 } },
      host_ip: "192.0.2.5",
      description: "Weekend game",
      node: 2,
    });
  });

  it("validates each step", () => {
    const state = initialGameState(game);
    expect(validateGameStep("software", game, state, "")).toEqual({ version: "Choose a version" });
    expect(validateGameStep("resources", game, { ...state, memory: "100", ports: { game: "80" } }, "2.1.24")).toEqual({
      memoryMb: "Memory must be at least 512 MB",
      "ports.game": "A port between 1024 and 65535, or empty for automatic",
    });
    expect(
      validateGameStep("settings", game, { ...state, settings: { ...state.settings, "map-gen-settings": { seed: "x" } } }, "2.1.24"),
    ).toEqual({ "settings.map-gen-settings.seed": "Enter a whole number" });
    const withSecret = factorioGame({ secrets: [{ key: "token", label: "Token", required: true }] });
    expect(validateGameStep("review", withSecret, initialGameState(withSecret), "2.1.24")).toEqual({ "secrets.token": "Token is required" });
  });

  it("maps server errors to their step", () => {
    expect(gameStepForField("version")).toBe("software");
    expect(gameStepForField("memory_mb")).toBe("resources");
    expect(gameStepForField("ports")).toBe("resources");
    expect(gameStepForField("settings")).toBe("settings");
    expect(gameStepForField("secrets")).toBe("review");
  });
});
