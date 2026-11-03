import { describe, expect, it } from "vitest";
import { formatBytes, formatDuration, formatMemoryMb, formatNumber, relativeTime } from "./format";

describe("format helpers", () => {
  it("formats bytes with binary units", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(5 * 1024 ** 3)).toBe("5.0 GB");
    expect(formatBytes(null)).toBe("—");
  });

  it("formats memory and counts", () => {
    expect(formatMemoryMb(512)).toBe("512 MB");
    expect(formatMemoryMb(6144)).toBe("6 GB");
    expect(formatMemoryMb(1536)).toBe("1.5 GB");
    expect(formatNumber(2_354_047)).toBe("2.4M");
    expect(formatNumber(999)).toBe("999");
  });

  it("formats durations", () => {
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(3700)).toBe("1h 1m");
    expect(formatDuration(90061)).toBe("1d 1h");
  });

  it("formats relative times in both directions", () => {
    const now = Date.parse("2026-11-03T12:00:00Z");
    expect(relativeTime("2026-11-03T11:59:50Z", now)).toBe("just now");
    expect(relativeTime("2026-11-03T11:00:00Z", now)).toBe("1 hour ago");
    expect(relativeTime("2026-11-01T12:00:00Z", now)).toBe("2 days ago");
    expect(relativeTime("2026-11-03T14:00:00Z", now)).toBe("in 2 hours");
    expect(relativeTime(null, now)).toBe("—");
  });
});
