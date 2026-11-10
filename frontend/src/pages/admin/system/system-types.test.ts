import { describe, expect, it } from "vitest";
import { isIpAddress, unreadableSecrets } from "./system-types";

describe("unreadableSecrets", () => {
  it("lists the stored secrets the panel cannot decrypt", () => {
    expect(unreadableSecrets({ secrets_unreadable: { curseforge_api_key: true } })).toEqual(["CurseForge API key"]);
    expect(unreadableSecrets({ secrets_unreadable: { curseforge_api_key: false } })).toEqual([]);
    expect(unreadableSecrets({ secrets_unreadable: { future_secret: true } })).toEqual(["future_secret"]);
    expect(unreadableSecrets({})).toEqual([]);
  });
});

describe("isIpAddress (default bind address)", () => {
  it("accepts plain IPv4 and IPv6 addresses", () => {
    for (const ok of ["0.0.0.0", "192.168.1.10", "::", "::1", "fe80::1", "2001:db8::8a2e:370:7334"]) expect(isIpAddress(ok)).toBe(true);
  });

  it("rejects an address with a port and other look-alikes the database refuses", () => {
    for (const bad of ["192.168.1.10:8080", "[::1]:25565", "1.2.3", "256.1.1.1", ":::", "a:b:c:d:e:f:g:h:i", "localhost", ""])
      expect(isIpAddress(bad)).toBe(false);
  });
});
