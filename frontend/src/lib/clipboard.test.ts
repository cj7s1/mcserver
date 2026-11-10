import { afterEach, describe, expect, it, vi } from "vitest";
import { copyText } from "./clipboard";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("copyText", () => {
  it("falls back to execCommand on a plain-HTTP origin (no async Clipboard API)", async () => {
    vi.stubGlobal("isSecureContext", false);
    const exec = vi.fn(() => true);
    document.execCommand = exec as unknown as typeof document.execCommand;
    await expect(copyText("world/level.dat")).resolves.toBe(true);
    expect(exec).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea")).toBeNull(); // cleaned up
  });

  it("uses the Clipboard API in a secure context", async () => {
    vi.stubGlobal("isSecureContext", true);
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    await expect(copyText("server.properties")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("server.properties");
  });

  it("reports failure instead of throwing", async () => {
    vi.stubGlobal("isSecureContext", false);
    document.execCommand = (() => {
      throw new Error("blocked");
    }) as unknown as typeof document.execCommand;
    await expect(copyText("x")).resolves.toBe(false);
  });
});
