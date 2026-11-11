import { describe, expect, it } from "vitest";
import { diffTexts, displayValue, fromText, groupFields, initialTexts, MASK, toText, valuesForCreate } from "./game-config";
import { consoleRules, isStopWord, MINECRAFT_CONSOLE, normaliseConsoleCommand } from "./games";
import type { ConfigField } from "./types";

const F = {
  name: { key: "name", type: "string", label: "Name" },
  pw: { key: "game_password", type: "string", label: "Password", secret: true },
  max: { key: "max_players", type: "int", label: "Max players", min: 0, max: 100, default: 0 },
  pub: { key: "visibility.public", type: "bool", label: "Public", default: false },
  cmds: { key: "allow_commands", type: "enum", label: "Commands", options: ["true", "false", "admins-only"] },
  seed: { key: "seed", type: "int", label: "Seed", nullable: true, default: null },
  tags: { key: "tags", type: "list", label: "Tags" },
  ratio: { key: "ratio", type: "number", label: "Ratio", min: 0 },
} satisfies Record<string, ConfigField>;

describe("field texts and values", () => {
  it("turns values into texts and back, by type", () => {
    expect(toText(F.pub, true)).toBe("true");
    expect(toText(F.seed, null)).toBe("");
    expect(toText(F.tags, ["a", "b"])).toBe("a\nb");
    expect(fromText(F.pub, "false")).toEqual({ value: false });
    expect(fromText(F.max, "12")).toEqual({ value: 12 });
    expect(fromText(F.max, "12.5")).toEqual({ error: "Enter a whole number" });
    expect(fromText(F.max, "101")).toEqual({ error: "Must be at most 100" });
    expect(fromText(F.max, "")).toEqual({ error: "Enter a number" });
    expect(fromText(F.seed, "")).toEqual({ value: null });
    expect(fromText(F.ratio, "1.5")).toEqual({ value: 1.5 });
    expect(fromText(F.cmds, "admins-only")).toEqual({ value: "admins-only" });
    expect(fromText(F.cmds, "sometimes")).toHaveProperty("error");
    expect(fromText(F.tags, "a\n\n b \n")).toEqual({ value: ["a", "b"] });
    expect(fromText(F.name, "two\nlines")).toEqual({ error: "Must be a single line" });
  });

  it("starts from the stored values, else the defaults", () => {
    expect(initialTexts([F.max, F.pub, F.seed], { max_players: 8 })).toEqual({ max_players: "8", "visibility.public": "false", seed: "" });
  });

  it("never sends back a masked secret, but sends one the user typed", () => {
    const fields = [F.name, F.pw, F.max];
    const before = initialTexts(fields, { name: "Factory", game_password: MASK, max_players: 0 });
    expect(diffTexts(fields, before, { ...before, max_players: "10" })).toEqual({ changes: { max_players: 10 }, errors: {} });
    expect(diffTexts(fields, before, { ...before, game_password: "s3cret" }).changes).toEqual({ game_password: "s3cret" });
    // typing and then restoring the mask is no change either
    expect(diffTexts(fields, before, { ...before, game_password: MASK }).changes).toEqual({});
  });

  it("reports invalid input instead of sending it", () => {
    const before = initialTexts([F.max]);
    expect(diffTexts([F.max], before, { max_players: "lots" })).toEqual({ changes: {}, errors: { max_players: "Enter a whole number" } });
  });

  it("sends only what differs from the defaults when creating", () => {
    const fields = [F.pub, F.max, F.seed];
    expect(valuesForCreate(fields, { "visibility.public": "false", max_players: "0", seed: "" }).changes).toEqual({});
    expect(valuesForCreate(fields, { "visibility.public": "true", max_players: "0", seed: "123" }).changes).toEqual({
      "visibility.public": true,
      seed: 123,
    });
  });

  it("groups fields in order and shows values in summaries", () => {
    const grouped = groupFields([
      { ...F.name, group: "General" },
      { ...F.pub, group: "Visibility" },
      { ...F.max, group: "General" },
    ]);
    expect(grouped.map(([g, list]) => [g, list.map((f) => f.key)])).toEqual([
      ["General", ["name", "max_players"]],
      ["Visibility", ["visibility.public"]],
    ]);
    expect(displayValue(F.pub, true)).toBe("On");
    expect(displayValue(F.pw, MASK)).toBe("Set");
    expect(displayValue(F.seed, null)).toBe("Random / not set");
  });
});

describe("console rules", () => {
  const factorio = consoleRules({ id: "factorio", stop_words: ["/quit"], keeps_command_slash: true, command_hint: "/players online" });

  it("Minecraft strips leading slashes and stops on 'stop' (as before)", () => {
    expect(consoleRules(undefined)).toBe(MINECRAFT_CONSOLE);
    expect(consoleRules({ id: "minecraft" })).toBe(MINECRAFT_CONSOLE);
    expect(normaliseConsoleCommand("  //say hi ")).toBe("say hi");
    expect(isStopWord("STOP")).toBe(true);
    expect(isStopWord("/quit")).toBe(false);
  });

  it("Factorio keeps its slash and stops on /quit", () => {
    expect(normaliseConsoleCommand(" /players online ", factorio)).toBe("/players online");
    expect(normaliseConsoleCommand("hello everyone", factorio)).toBe("hello everyone");
    expect(isStopWord("/QUIT", factorio)).toBe(true);
    expect(isStopWord("stop", factorio)).toBe(false);
    expect(factorio.example).toBe("/players online");
  });
});
