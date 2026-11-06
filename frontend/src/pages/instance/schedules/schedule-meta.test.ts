import { describe, expect, it } from "vitest";
import type { Schedule } from "@/lib/types";
import type { PermissionCode } from "@/lib/types";
import {
  actionBlockedReason,
  DEFAULT_WARN_MINUTES,
  draftFrom,
  editBlockedReason,
  SCHEDULE_ACTIONS,
  scheduleWarnMinutes,
  warnPayload,
} from "./schedule-meta";

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

describe("schedule action permissions (backend SCHEDULE_ACTION_PERMISSIONS)", () => {
  const canOnly =
    (...codes: PermissionCode[]) =>
    (code: PermissionCode) =>
      codes.includes(code);

  it("offers only the actions the user may run", () => {
    const allowed = (can: (c: PermissionCode) => boolean) => SCHEDULE_ACTIONS.filter((a) => !actionBlockedReason(a, can));
    expect(allowed(canOnly("instance.schedules"))).toEqual([]);
    expect(allowed(canOnly("instance.control"))).toEqual(["restart", "start", "stop"]);
    expect(allowed(canOnly("instance.backup"))).toEqual(["backup"]);
    expect(allowed(canOnly("instance.backup", "instance.restore"))).toEqual(["backup", "snapshot"]);
    expect(allowed(canOnly("instance.command"))).toEqual(["command"]);
    expect(actionBlockedReason("command", canOnly("instance.control"))).toMatch(/Send commands/);
  });

  it("needs restore permission to edit any snapshot schedule (all of them prune)", () => {
    const backup = canOnly("instance.backup");
    expect(editBlockedReason(schedule({ keep: 5 }, "snapshot" as Schedule["action"]), backup)).toMatch(/Restore/);
    expect(editBlockedReason(schedule({}, "snapshot" as Schedule["action"]), backup)).toMatch(/Restore/);
    expect(editBlockedReason(schedule({ keep: 5 }, "snapshot" as Schedule["action"]), canOnly("instance.backup", "instance.restore"))).toBeNull();
    expect(editBlockedReason(schedule({ command: "say hi" }, "command"), backup)).toMatch(/^Editing command schedules needs/);
  });
});
