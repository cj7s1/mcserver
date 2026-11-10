import { describe, expect, it } from "vitest";
import { cronError } from "./cron";

describe("cronError: dates that never occur", () => {
  it("rejects a day of month that none of the listed months has", () => {
    expect(cronError("0 0 31 2 *")).toBe("February has no day 31, so this schedule would never run.");
    expect(cronError("0 4 30 2 *")).toMatch(/February has no day 30/);
    expect(cronError("0 4 31 4 *")).toMatch(/April has no day 31/);
    expect(cronError("0 4 31 4,6,9,11 *")).toMatch(/^April, June, September and November have no day 31/);
    expect(cronError("0 4 31 feb *")).toMatch(/February/);
  });

  it("accepts dates that exist in at least one listed month (29 February in leap years)", () => {
    expect(cronError("0 0 29 2 *")).toBeNull();
    expect(cronError("0 0 31 1,2 *")).toBeNull();
    expect(cronError("0 0 31 * *")).toBeNull();
    expect(cronError("0 4 30-31 4,5 *")).toBeNull();
  });

  it("leaves weekday combinations alone (cron runs on either match)", () => {
    expect(cronError("0 0 31 2 1")).toBeNull();
  });

  it("still reports the existing syntax errors first", () => {
    expect(cronError("0 0 32 2 *")).toMatch(/between 1 and 31/);
    expect(cronError("0 0 31 2")).toMatch(/Use 5 fields/);
  });
});
