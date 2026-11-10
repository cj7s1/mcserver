import { describe, expect, it } from "vitest";
import { buildPayload, CF_REF, type Derived, initialState, validateStep } from "./wizardModel";

const d = { isModpack: true, name: "Better MC", memoryMb: 8192, gamePort: "25565", resolveOk: false, resolvePending: false } as unknown as Derived;

describe("CurseForge by page URL (no API key)", () => {
  it("accepts page URLs, file URLs and slugs; rejects other hosts", () => {
    expect(CF_REF.test("https://www.curseforge.com/minecraft/modpacks/better-mc-forge-bmc4")).toBe(true);
    expect(CF_REF.test("https://www.curseforge.com/minecraft/modpacks/better-mc-forge-bmc4/files/9104992")).toBe(true);
    expect(CF_REF.test("better-mc-fabric-bmc2")).toBe(true);
    expect(CF_REF.test("https://evil.example/minecraft/modpacks/x")).toBe(false);
  });

  it("validates the URL form and builds the keyless payload", () => {
    const state = { ...initialState(), category: "modpack" as const, modpackSource: "url" as const, acceptEula: true };
    expect(Object.keys(validateStep("software", state, d))).toEqual(expect.arrayContaining(["cfRef", "cfMinecraftVersion"]));
    const filled = {
      ...state,
      cfRef: "https://www.curseforge.com/minecraft/modpacks/better-mc-forge-bmc4",
      cfMinecraftVersion: "1.20.1",
      cfFileId: "9104992",
    };
    expect(validateStep("software", filled, d)).toEqual({});
    const payload = buildPayload(filled, d);
    expect(payload).toMatchObject({
      mode: "modpack",
      modpack_provider: "curseforge",
      modpack_project_id: filled.cfRef,
      modpack_version_id: "9104992",
      minecraft_version: "1.20.1",
    });
  });
});

describe("nightly backup option", () => {
  const standard = {
    isModpack: false,
    serverType: "PAPER",
    minecraftVersion: "1.21.1",
    loaderVersion: "",
    name: "Lobby",
    memoryMb: 4096,
    gamePort: "25570",
    nightlyBackupAllowed: true,
  } as unknown as Derived;

  it("is on by default and sent with the server for normal, modpack and imported servers", () => {
    expect(initialState().nightlyBackup).toBe(true);
    const state = { ...initialState(), category: "plugins" as const, acceptEula: true };
    expect(buildPayload(state, standard)).toMatchObject({ mode: "standard", start: true, nightly_backup: true });
    expect(buildPayload({ ...state, category: "import" as const }, standard)).toMatchObject({
      mode: "standard",
      start: false, // an import stays stopped, but it is backed up like any other server
      nightly_backup: true,
    });
    const modpack = { ...standard, isModpack: true } as Derived;
    expect(buildPayload({ ...state, category: "modpack" as const }, modpack)).toMatchObject({ mode: "modpack", nightly_backup: true });
  });

  it("sends false when the user switches it off", () => {
    const state = { ...initialState(), category: "plugins" as const, nightlyBackup: false };
    expect(buildPayload(state, standard).nightly_backup).toBe(false);
  });

  it("never asks for a schedule while the option is hidden", () => {
    const state = { ...initialState(), category: "plugins" as const };
    expect(buildPayload(state, { ...standard, nightlyBackupAllowed: false }).nightly_backup).toBe(false);
  });

  it("is offered to users who may schedule backups on the server they create", async () => {
    const { nightlyBackupAllowed } = await import("./wizardModel");
    expect(nightlyBackupAllowed(null)).toBe(false);
    expect(nightlyBackupAllowed({ is_owner: true, permissions: [] })).toBe(true);
    // without global "View server" the creator becomes Server manager of the new server
    expect(nightlyBackupAllowed({ is_owner: false, permissions: ["instance.create"] })).toBe(true);
    // with it, only the user's own role counts: both "Manage schedules" and "Create backups"
    const globalView = (...more: string[]) =>
      nightlyBackupAllowed({ is_owner: false, permissions: ["instance.create", "instance.view", ...more] as never });
    expect(globalView()).toBe(false);
    expect(globalView("instance.backup")).toBe(false);
    expect(globalView("instance.schedules")).toBe(false);
    expect(globalView("instance.schedules", "instance.backup")).toBe(true);
  });

  it("maps a backend error on the field to the resources step", async () => {
    const { SERVER_FIELD_MAP, stepForField } = await import("./wizardModel");
    expect(stepForField(SERVER_FIELD_MAP.nightly_backup)).toBe("resources");
  });
});

describe("container memory limit", () => {
  it("matches the backend formula", async () => {
    const { containerLimitMb } = await import("./wizardModel");
    expect(containerLimitMb(6144, true)).toBe(6144 + 2457); // mods: 40%
    expect(containerLimitMb(2048, true)).toBe(2048 + 1536); // mods: at least 1.5 GB
    expect(containerLimitMb(4096, false)).toBe(4096 + 1228); // others: 30%
    expect(containerLimitMb(1024, false)).toBe(1024 + 768);
  });
});

describe("environment variable errors", () => {
  it("leaves download/command variables to the backend and shows its message on the env field", async () => {
    const { MANAGED_ENV, SERVER_FIELD_MAP, stepForField } = await import("./wizardModel");
    // the client hint only knows panel-managed keys; the backend's precise reason for e.g. MODS
    // ("is not allowed: it would install unverified code ...") arrives as a field error on "env"
    expect(MANAGED_ENV.has("TYPE")).toBe(true);
    expect(MANAGED_ENV.has("MODS")).toBe(false);
    expect(SERVER_FIELD_MAP.env).toBe("env");
    expect(stepForField(SERVER_FIELD_MAP.env)).toBe("gameplay");
  });
});
