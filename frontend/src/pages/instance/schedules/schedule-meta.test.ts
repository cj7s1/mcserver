import { describe, expect, it } from "vitest";
import type { Schedule } from "@/lib/types";
import { DEFAULT_WARN_MINUTES, draftFrom, scheduleWarnMinutes, warnPayload } from "./schedule-meta";

function schedule(payload: Record<string, unknown>, action: Schedule["action"] = "restart"): Schedule {
  return {
    id: 1,
    name: "Nightly",
    action,
    cron: "0 4 * * *",
    payload,
    enabled: true,
    only_when_running: true,
    last_run_at: null,
    next_run_at: null,
    last_result: "",
    created_at: "2026-11-06T00:00:00Z",
  };
}

describe("schedule warning fields", () => {
  it("defaults new schedules to a warning and keeps existing ones as saved", () => {
    expect(draftFrom(null).warnMinutes).toBe(DEFAULT_WARN_MINUTES);
    expect(draftFrom(schedule({})).warnMinutes).toBe(0); // saved before the option existed
    const d = draftFrom(schedule({ warn_minutes: 10, warn_message: "Daily" }));
    expect([d.warnMinutes, d.warnMessage]).toEqual([10, "Daily"]);
    expect(draftFrom(null, { warnMinutes: 1 }).warnMinutes).toBe(1);
  });

  it("reads only valid stored periods", () => {
    expect(scheduleWarnMinutes(schedule({ warn_minutes: 15 }))).toBe(15);
    expect(scheduleWarnMinutes(schedule({ warn_minutes: "5" }))).toBe(5);
    expect(scheduleWarnMinutes(schedule({ warn_minutes: 7 }))).toBeNull();
    expect(scheduleWarnMinutes(schedule({}))).toBeNull();
  });

  it("builds the payload", () => {
    expect(warnPayload({}, { warnMinutes: 5, warnMessage: " Daily restart " })).toEqual({
      warn_minutes: 5,
      warn_message: "Daily restart",
    });
    // no warning: the message is dropped, unrelated keys are kept
    expect(warnPayload({ other: 1, warn_message: "old" }, { warnMinutes: 0, warnMessage: "x" })).toEqual({
      other: 1,
      warn_minutes: 0,
    });
    expect(warnPayload({}, { warnMinutes: 9, warnMessage: "" })).toEqual({ warn_minutes: 0 });
  });
});
