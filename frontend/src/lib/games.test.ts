import { describe, expect, it } from "vitest";
import { makeInstance, withGameFields } from "@/test/fixtures";
import {
  containerMemoryMb,
  gameLabel,
  hasCapability,
  instanceCapabilities,
  knownVariantLabel,
  loaderLabel,
  memoryModel,
  memoryWording,
  variantColor,
  variantLabel,
} from "./games";
import { defaultSettingsTab, instanceSections, overviewShortcuts, settingsTabs } from "./sections";
import type { Instance } from "./types";

const KINDS: Partial<Instance>[] = [
  {},
  { server_type: "PAPER", content_kind: "plugins" },
  { server_type: "FABRIC", content_kind: "mods" },
  { server_type: "MODRINTH", content_kind: "mods", is_modpack: true },
  { server_type: "CUSTOM" },
];

describe("instance capabilities", () => {
  it.each(KINDS)("an older panel's Minecraft server gets what the backend would send (%o)", (over) => {
    const i = makeInstance(over);
    expect([...instanceCapabilities(i)].sort()).toEqual(withGameFields(i).capabilities);
  });

  it("uses the API's list when there is one", () => {
    const i = { ...makeInstance({ content_kind: "mods" }), capabilities: ["console_commands" as const] };
    expect(hasCapability(i, "console_commands")).toBe(true);
    expect(hasCapability(i, "content")).toBe(false);
    expect(hasCapability(i, "players")).toBe(false);
  });

  it("gives another game without a list nothing (only new panels create those)", () => {
    expect(instanceCapabilities({ ...makeInstance(), game: "factorio" }).size).toBe(0);
  });
});

describe("sections for a game with fewer capabilities", () => {
  const factorio: Instance = {
    ...makeInstance({ permissions: ["instance.view", "instance.console", "instance.files.read", "instance.backup", "instance.access"] }),
    game: "factorio",
    game_label: "Factorio",
    capabilities: ["console_commands", "software_update"],
    memory_model: "plain",
  };

  it("hides what needs a missing capability and keeps the rest", () => {
    expect(instanceSections(factorio).map((s) => s.label(factorio))).toEqual([
      "Overview",
      "Console",
      "Files",
      "Backups",
      "Schedules",
      "Network",
      "Settings",
      "Access",
      "Activity",
    ]);
    expect(overviewShortcuts(factorio).map((s) => s.section.key)).toEqual(["console", "files", "backups"]);
    expect(settingsTabs(factorio).map((t) => t.label(factorio))).toEqual(["Resources", "Software", "Environment", "General", "Danger zone"]);
    expect(defaultSettingsTab(factorio)).toBe("resources");
  });

  it("names the game", () => {
    expect(gameLabel(factorio)).toBe("Factorio");
    expect(gameLabel(makeInstance())).toBe("Minecraft");
    expect(gameLabel("minecraft")).toBe("Minecraft");
    expect(gameLabel({ game: "terraria" })).toBe("Terraria");
  });
});

describe("variant labels and colours", () => {
  // the copies this helper replaced (library/server-utils, overview-utils, wizardModel, ServerIcon)
  const LABELS: Record<string, string> = {
    VANILLA: "Vanilla",
    PAPER: "Paper",
    PURPUR: "Purpur",
    FOLIA: "Folia",
    FABRIC: "Fabric",
    QUILT: "Quilt",
    FORGE: "Forge",
    NEOFORGE: "NeoForge",
    MODRINTH: "Modrinth modpack",
    AUTO_CURSEFORGE: "CurseForge modpack",
    FTBA: "FTB modpack",
    CUSTOM: "Custom",
  };
  const COLORS: Record<string, string> = {
    VANILLA: "from-emerald-500/30 to-emerald-700/30 text-emerald-700 dark:text-emerald-300",
    PAPER: "from-sky-500/30 to-sky-700/30 text-sky-700 dark:text-sky-300",
    PURPUR: "from-violet-500/30 to-violet-700/30 text-violet-700 dark:text-violet-300",
    FOLIA: "from-teal-500/30 to-teal-700/30 text-teal-700 dark:text-teal-300",
    FABRIC: "from-amber-500/30 to-amber-700/30 text-amber-700 dark:text-amber-200",
    QUILT: "from-fuchsia-500/30 to-fuchsia-700/30 text-fuchsia-700 dark:text-fuchsia-300",
    FORGE: "from-orange-500/30 to-orange-800/30 text-orange-700 dark:text-orange-300",
    NEOFORGE: "from-orange-400/30 to-red-700/30 text-orange-700 dark:text-orange-200",
  };
  const GREY = "from-zinc-500/30 to-zinc-700/30 text-zinc-700 dark:text-zinc-300";

  it("keeps every label and colour", () => {
    for (const [id, label] of Object.entries(LABELS)) {
      expect(variantLabel(id)).toBe(label);
      expect(knownVariantLabel(id)).toBe(label);
    }
    for (const id of Object.keys(LABELS)) expect(variantColor(id)).toBe(COLORS[id] ?? GREY);
    expect(variantColor(undefined)).toBe(GREY);
    expect(variantLabel("SPONGE")).toBe("Sponge");
    expect(knownVariantLabel("SPONGE")).toBeUndefined();
  });

  it("labels loaders as before", () => {
    expect(loaderLabel("")).toBe("—");
    expect(loaderLabel("neoforge")).toBe("NeoForge");
    expect(loaderLabel("modrinth")).toBe("Modrinth");
    expect(loaderLabel("liteloader")).toBe("Liteloader");
  });
});

describe("memory", () => {
  it("keeps the JVM formula (heap + max(768, 30%), modded heap + max(1536, 40%), or the set overhead)", () => {
    expect(containerMemoryMb(2048)).toBe(2048 + 768);
    expect(containerMemoryMb(8192)).toBe(8192 + 2457);
    expect(containerMemoryMb(2048, { modded: true })).toBe(2048 + 1536);
    expect(containerMemoryMb(8192, { modded: true })).toBe(8192 + 3276);
    expect(containerMemoryMb(4096, { overheadMb: 512 })).toBe(4608);
    expect(containerMemoryMb(4096, { model: "plain" })).toBe(4096);
    expect(containerMemoryMb(4096, { model: "plain", overheadMb: 256 })).toBe(4352);
  });

  it("uses heap wording only for the jvm model; older panels are jvm", () => {
    expect(memoryModel(makeInstance())).toBe("jvm");
    const jvm = memoryWording("jvm");
    expect(jvm.settingsDescription).toBe(
      "Java heap for the server. The container limit adds headroom for off-heap memory so the server is not OOM-killed.",
    );
    expect(jvm.wizardDescription).toBe("Java heap size. The container gets extra headroom for the JVM itself.");
    expect([jvm.fieldLabel, jvm.sliderLabel, jvm.shortLabel, jvm.inputLabel]).toEqual(["Heap size (-Xmx)", "Heap size", "Heap", "Heap (MB)"]);
    expect(jvm.autoOverheadHint(true)).toBe("40% of heap, at least 1.5 GB for modded servers");
    expect(jvm.autoOverheadHint(false)).toBe("30% of heap, at least 768 MB");
    expect(jvm.autoOverheadRule(true)).toBe("mods: 40%, at least 1.5 GB");
    expect(jvm.autoOverheadRule(false)).toBe("30%, at least 768 MB");
    const plain = memoryWording("plain");
    expect(JSON.stringify(Object.values(plain).filter((v) => typeof v === "string"))).not.toMatch(/heap|Java|JVM/i);
  });
});
