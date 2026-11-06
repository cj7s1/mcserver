import { describe, expect, it } from "vitest";
import { readResetToken } from "./AuthPages";

describe("readResetToken", () => {
  it("prefers the URL fragment and falls back to the legacy query", () => {
    expect(readResetToken("#token=abc123", null)).toBe("abc123");
    expect(readResetToken("#token=abc123", "old")).toBe("abc123");
    expect(readResetToken("", "old")).toBe("old");
    expect(readResetToken("#other=1", null)).toBe("");
    expect(readResetToken("token=xyz", null)).toBe("xyz");
  });
});
