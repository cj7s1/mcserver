import { describe, expect, it } from "vitest";
import {
  countdownText,
  describeWarning,
  isWarnMinutes,
  powerBody,
  warningMarks,
  warnMessageError,
  warnOptionLabel,
} from "./power-warning";

describe("power warnings", () => {
  it("mirrors the backend warning marks", () => {
    expect(warningMarks(0)).toEqual([]);
    expect(warningMarks(1)).toEqual([60, 10]);
    expect(warningMarks(5)).toEqual([300, 60, 10]);
    expect(warningMarks(15)).toEqual([900, 600, 300, 60, 10]);
    expect(describeWarning(0)).toBe("Players are not warned.");
    expect(describeWarning(5)).toBe("Players are told in chat 5 min, 1 min and 10 s before.");
    expect(warnOptionLabel(0)).toBe("No warning");
    expect(warnOptionLabel(1)).toBe("Warn players 1 minute before");
    expect(warnOptionLabel(10)).toBe("Warn players 10 minutes before");
  });

  it("accepts only the allowed periods", () => {
    expect([0, 1, 5, 10, 15].every(isWarnMinutes)).toBe(true);
    expect(isWarnMinutes(7)).toBe(false);
    expect(isWarnMinutes("5")).toBe(false);
  });

  it("validates the optional message like the backend", () => {
    expect(warnMessageError("")).toBeNull();
    expect(warnMessageError("  Daily restart  ")).toBeNull();
    expect(warnMessageError("two\nlines")).toMatch(/single line/);
    expect(warnMessageError("bell\u0007")).toMatch(/single line/);
    expect(warnMessageError("sep x")).toMatch(/single line/);
    expect(warnMessageError("§cred")).toMatch(/Formatting codes/);
    expect(warnMessageError("x".repeat(101))).toMatch(/At most 100/);
  });

  it("formats the remaining time", () => {
    const now = Date.parse("2026-11-06T12:00:00Z");
    expect(countdownText("2026-11-06T12:04:05Z", now)).toBe("4:05");
    expect(countdownText("2026-11-06T12:00:00.400Z", now)).toBe("0:01");
    expect(countdownText("2026-11-06T11:59:00Z", now)).toBe("0:00");
    expect(countdownText(null, now)).toBeNull();
    expect(countdownText("garbage", now)).toBeNull();
  });

  it("builds the power request body", () => {
    expect(powerBody("restart", 0, "x")).toEqual({ action: "restart" });
    expect(powerBody("restart", 5, "  ")).toEqual({ action: "restart", warn_minutes: 5 });
    expect(powerBody("stop", 10, " Bye ")).toEqual({ action: "stop", warn_minutes: 10, warn_message: "Bye" });
    expect(powerBody("start", 5, "x")).toEqual({ action: "start" });
  });
});
