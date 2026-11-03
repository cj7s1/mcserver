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
