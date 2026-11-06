import { describe, expect, it } from "vitest";
import { unreadableSecrets } from "./system-types";

describe("unreadableSecrets", () => {
  it("lists the stored secrets the panel cannot decrypt", () => {
    expect(unreadableSecrets({ secrets_unreadable: { curseforge_api_key: true } })).toEqual(["CurseForge API key"]);
    expect(unreadableSecrets({ secrets_unreadable: { curseforge_api_key: false } })).toEqual([]);
    expect(unreadableSecrets({ secrets_unreadable: { future_secret: true } })).toEqual(["future_secret"]);
    expect(unreadableSecrets({})).toEqual([]);
  });
});
