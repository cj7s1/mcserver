import { describe, expect, it } from "vitest";
import type { Job } from "@/lib/types";
import { makeBukkitResetPlan, makeResetFolder, makeResetPlan } from "@/test/fixtures";
import {
  buildResetBody,
  deletedFolders,
  initialForm,
  keepChoices,
  outcomeMessage,
  PLAN_OUTDATED_CODES,
  resolveForm,
  seedError,
  seedFromEnv,
  seedIsSet,
  summaryLines,
  summaryText,
  type ResetForm,
  type WorldResetResult,
} from "./reset-world-model";

const form = (plan = makeResetPlan(), over: Partial<ResetForm> = {}): ResetForm => ({ ...initialForm(plan), ...over });

describe("initialForm", () => {
  it("preselects a random seed when none is set, and keeps a configured one", () => {
    expect(initialForm(makeResetPlan()).seedMode).toBe("random");
    expect(initialForm(makeResetPlan({ seed: { value: "42", source: "server.properties" } })).seedMode).toBe("keep");
    expect(initialForm(makeResetPlan({ seed: { value: "42", source: "env" } })).seedMode).toBe("keep");
  });

  it("carries everything over and takes the backup default from the panel setting", () => {
    expect(initialForm(makeResetPlan())).toEqual({ seedMode: "random", seed: "", keep: { datapacks: true, config: true }, backup: true });
    expect(initialForm(makeResetPlan({ pre_restore_backup_default: false })).backup).toBe(false);
  });
});

describe("seed state of a plan", () => {
  it("tells a set seed from an unset one, and the environment from server.properties", () => {
    expect(seedIsSet(makeResetPlan())).toBe(false);
    expect(seedIsSet(makeResetPlan({ seed: { value: "", source: "server.properties" } }))).toBe(false);
    expect(seedIsSet(makeResetPlan({ seed: { value: "7", source: "server.properties" } }))).toBe(true);
    expect(seedFromEnv(makeResetPlan({ seed: { value: "7", source: "server.properties" } }))).toBe(false);
    expect(seedFromEnv(makeResetPlan({ seed: { value: "7", source: "env" } }))).toBe(true);
  });

  it("resolves the form against the plan", () => {
    const env = makeResetPlan({ seed: { value: "7", source: "env" } });
    expect(resolveForm(env, form(env, { seedMode: "custom", seed: "abc" })).seedMode).toBe("keep");
    // "keep" is not offered without a seed
    expect(resolveForm(makeResetPlan(), form(makeResetPlan(), { seedMode: "keep" })).seedMode).toBe("random");
    const unchanged = form(makeResetPlan(), { seedMode: "custom", seed: "abc" });
    expect(resolveForm(makeResetPlan(), unchanged)).toBe(unchanged);
  });
});

describe("seedError", () => {
  it("only checks a custom seed", () => {
    expect(seedError({ seedMode: "keep", seed: "" })).toBeNull();
    expect(seedError({ seedMode: "random", seed: "\n" })).toBeNull();
  });

  it("needs 1 to 200 characters after trimming", () => {
    expect(seedError({ seedMode: "custom", seed: "" })).toMatch(/Enter a seed/);
    expect(seedError({ seedMode: "custom", seed: "   " })).toMatch(/Enter a seed/);
    expect(seedError({ seedMode: "custom", seed: "x" })).toBeNull();
    expect(seedError({ seedMode: "custom", seed: "-4172144997902289642" })).toBeNull();
    expect(seedError({ seedMode: "custom", seed: "My Seed with spaces" })).toBeNull();
    expect(seedError({ seedMode: "custom", seed: "x".repeat(200) })).toBeNull();
    expect(seedError({ seedMode: "custom", seed: `  ${"x".repeat(200)}  ` })).toBeNull();
    expect(seedError({ seedMode: "custom", seed: "x".repeat(201) })).toMatch(/at most 200 characters; this one has 201/);
    // counted in characters, as the backend does (an emoji is two UTF-16 units)
    expect(seedError({ seedMode: "custom", seed: "🌍".repeat(200) })).toBeNull();
  });

  it("refuses control characters", () => {
    // the backend refuses Unicode categories Cc, Zl and Zp
    for (const seed of ["a\nb", "a\tb", "a\u0000b", "a\u007fb", "a\u0085b", "a\u2028b", "a\u2029b"]) {
      expect(seedError({ seedMode: "custom", seed }), JSON.stringify(seed)).toMatch(/control characters/);
    }
  });
});

describe("keepChoices and deletedFolders", () => {
  it("offers only what exists in the folders that are deleted", () => {
    expect(keepChoices(makeResetPlan())).toEqual([]);
    expect(keepChoices(makeBukkitResetPlan())).toEqual([
      { option: "datapacks", paths: ["bruh/datapacks"] },
      { option: "config", paths: ["bruh/paper-world.yml", "bruh_nether/paper-world.yml"] },
    ]);
    // a folder that is not deleted (only carry-overs of an earlier reset are left in it) has nothing to carry over
    const pending = makeResetPlan({
      folders: [makeResetFolder({ state: "pending", markers: [], keepable: { datapacks: ["datapacks"] } })],
      delete: [],
    });
    expect(keepChoices(pending)).toEqual([]);
  });

  it("lists the deleted folders in the plan's order with their details", () => {
    const plan = makeBukkitResetPlan({ delete: ["bruh_nether", "bruh", "gone"] });
    expect(deletedFolders(plan).map((d) => [d.path, d.info?.role ?? null])).toEqual([
      ["bruh_nether", "nether"],
      ["bruh", "overworld"],
      ["gone", null],
    ]);
  });
});

describe("buildResetBody", () => {
  it("takes the folders and the running state from the plan", () => {
    const plan = makeBukkitResetPlan();
    expect(buildResetBody(plan, form(plan))).toEqual({
      folders: ["bruh", "bruh_nether", "bruh_the_end"],
      stop_if_running: true,
      pre_restore_backup: true,
      seed_mode: "keep",
      keep: ["datapacks", "config"],
    });
    const stopped = makeResetPlan();
    expect(buildResetBody(stopped, form(stopped))).toEqual({
      folders: ["world"],
      stop_if_running: false,
      pre_restore_backup: true,
      seed_mode: "random",
      keep: [],
    });
    // a copy: the request cannot change the plan
    expect(buildResetBody(plan, form(plan)).folders).not.toBe(plan.delete);
  });

  it("sends the seed only for a custom seed, trimmed", () => {
    const plan = makeBukkitResetPlan();
    expect(buildResetBody(plan, form(plan, { seedMode: "keep", seed: "left over" }))).not.toHaveProperty("seed");
    expect(buildResetBody(plan, form(plan, { seedMode: "random", seed: "left over" }))).toMatchObject({ seed_mode: "random" });
    expect(buildResetBody(plan, form(plan, { seedMode: "random", seed: "left over" }))).not.toHaveProperty("seed");
    expect(buildResetBody(plan, form(plan, { seedMode: "custom", seed: "  Glacier 12  " }))).toMatchObject({ seed_mode: "custom", seed: "Glacier 12" });
  });

  it("never asks for a seed change while SEED is in the environment", () => {
    const plan = makeResetPlan({ seed: { value: "7", source: "env" } });
    const body = buildResetBody(plan, form(plan, { seedMode: "custom", seed: "abc" }));
    expect(body.seed_mode).toBe("keep");
    expect(body).not.toHaveProperty("seed");
  });

  it("sends the carry-overs that are ticked and exist, and the backup choice", () => {
    const plan = makeBukkitResetPlan();
    expect(buildResetBody(plan, form(plan, { keep: { datapacks: false, config: true } })).keep).toEqual(["config"]);
    expect(buildResetBody(plan, form(plan, { keep: { datapacks: false, config: false } })).keep).toEqual([]);
    expect(buildResetBody(plan, form(plan, { backup: false })).pre_restore_backup).toBe(false);
    // ticked by default, but this world has no datapacks or config to carry over
    expect(buildResetBody(makeResetPlan(), form()).keep).toEqual([]);
  });
});

describe("PLAN_OUTDATED_CODES", () => {
  it("covers every reason the backend refuses a confirmed plan", () => {
    // backend apps/instances/world.py: the blocker codes of reset_plan(), plus the request checks of WorldResetView
    const blockers = [
      "invalid_level_name",
      "level_name_conflict",
      "world_location_overridden",
      "not_a_directory",
      "not_a_world",
      "no_world",
      "agent_outdated",
    ];
    expect([...PLAN_OUTDATED_CODES].sort()).toEqual([...blockers, "world_changed", "must_stop", "seed_env_set"].sort());
    // errors that say nothing about the plan keep the confirmation open
    for (const code of ["instance_busy", "archived", "confirmation_required", "validation_error", "agent_unavailable"]) {
      expect(PLAN_OUTDATED_CODES).not.toContain(code);
    }
  });
});

describe("summaryLines", () => {
  const text = (plan = makeResetPlan(), over: Partial<ResetForm> = {}) => summaryLines(plan, form(plan, over)).map(summaryText);

  it("lists stop, backup, delete, seed and start in order", () => {
    const plan = makeBukkitResetPlan();
    expect(summaryLines(plan, form(plan)).map((l) => l.id)).toEqual(["stop", "backup", "delete", "seed", "start"]);
    expect(text(plan)).toEqual([
      "The server is stopped (players are disconnected) and stays offline during the backup.",
      "A safety backup of the whole server folder is taken and checked (“Before restore”). If it fails or does not contain the world, nothing is deleted.",
      "The world folders bruh, bruh_nether and bruh_the_end (2.0 GB in total) are deleted, with all player data in them. " +
        "Carried over into the new world: bruh/datapacks, bruh/paper-world.yml and bruh_nether/paper-world.yml.",
      "The seed setting is kept (level-seed: -4172144997902289642): the new world is generated from this seed.",
      "The server is started again and generates the new world.",
    ]);
  });

  it("describes a stopped vanilla server", () => {
    expect(text()).toEqual([
      "The server stays stopped meanwhile.",
      "A safety backup of the whole server folder is taken and checked (“Before restore”). If it fails or does not contain the world, nothing is deleted.",
      "The world folder world (3.0 MB) is deleted, with all player data in it.",
      "The new world gets a random seed.",
      "The server is left stopped. It generates the new world the next time it starts.",
    ]);
  });

  it("warns when no safety backup is taken", () => {
    const plan = makeBukkitResetPlan();
    const lines = summaryLines(plan, form(plan, { backup: false }));
    expect(summaryText(lines[0])).toBe("The server is stopped (players are disconnected).");
    expect(summaryText(lines[1])).toBe("No safety backup is taken: the world cannot be recovered afterwards.");
    expect(lines[1].danger).toBe(true);
    expect(summaryLines(plan, form(plan)).some((l) => l.danger)).toBe(false);
  });

  it("says what is carried over", () => {
    const plan = makeBukkitResetPlan();
    expect(text(plan, { keep: { datapacks: true, config: false } })[2]).toMatch(/Carried over into the new world: bruh\/datapacks\.$/);
    expect(text(plan, { keep: { datapacks: false, config: false } })[2]).toMatch(/in them\. Nothing is carried over\.$/);
    // nothing to carry over: not mentioned
    expect(text()[2]).not.toMatch(/carried/i);
  });

  it("describes each seed choice", () => {
    const plan = makeBukkitResetPlan();
    expect(text(plan, { seedMode: "random" })[3]).toBe(
      "The seed setting is cleared (level-seed was -4172144997902289642): the new world gets a random seed.",
    );
    expect(text(plan, { seedMode: "custom", seed: " Glacier 12 " })[3]).toBe(
      "level-seed is set to Glacier 12: the new world is generated from this seed.",
    );
    const env = makeResetPlan({ seed: { value: "7", source: "env" } });
    expect(text(env, { seedMode: "custom", seed: "abc" })[3]).toBe(
      "The seed stays as the SEED environment variable sets it (7): the new world is generated from it.",
    );
    // SEED present but empty still decides (the backend refuses a seed change while it is set)
    const emptyEnv = makeResetPlan({ seed: { value: "", source: "env" } });
    expect(resolveForm(emptyEnv, form(emptyEnv)).seedMode).toBe("keep");
    expect(text(emptyEnv)[3]).toBe("The SEED environment variable is set but empty: the new world gets a random seed.");
    expect(buildResetBody(emptyEnv, form(emptyEnv, { seedMode: "random" })).seed_mode).toBe("keep");
  });

  it("marks folder names and values as code parts", () => {
    const plan = makeBukkitResetPlan();
    const codes = (id: string) =>
      summaryLines(plan, form(plan, { seedMode: "custom", seed: "abc" }))
        .find((l) => l.id === id)!
        .parts.filter((p) => typeof p !== "string")
        .map((p) => p.code);
    expect(codes("delete")).toEqual(["bruh", "bruh_nether", "bruh_the_end", "bruh/datapacks", "bruh/paper-world.yml", "bruh_nether/paper-world.yml"]);
    expect(codes("seed")).toEqual(["level-seed", "abc"]);
  });
});

describe("outcomeMessage", () => {
  const job = (
    over: Partial<Omit<Job, "result">> & { result?: WorldResetResult },
  ): Pick<Job, "kind" | "status" | "error" | "error_code" | "result"> => ({
    kind: "world_reset",
    status: "succeeded",
    error: "",
    error_code: "",
    ...over,
    result: (over.result ?? {}) as Record<string, unknown>,
  });
  const done: WorldResetResult = { deleted: ["world"], kept: [], skipped: [], leftover: null, warnings: [], safety_backup: null, restarted: false };

  it("is silent for running jobs and other kinds", () => {
    expect(outcomeMessage(job({ status: "running" }))).toBeNull();
    expect(outcomeMessage(job({ status: "queued" }))).toBeNull();
    expect(outcomeMessage(job({ kind: "world_replace" }))).toBeNull();
    expect(outcomeMessage(job({ kind: "migrate", status: "failed" }))).toBeNull();
  });

  it("reports a finished reset", () => {
    expect(outcomeMessage(job({ result: done }))).toEqual({
      tone: "success",
      title: "The world was reset",
      description: "The server generates a new world the next time it starts.",
    });
    const full = outcomeMessage(job({ result: { ...done, restarted: true, kept: ["world/datapacks"], safety_backup: "b1" } }));
    expect(full?.tone).toBe("success");
    expect(full?.description).toBe(
      "The server was started again and is generating a new world. Carried over: world/datapacks. " +
        "The old world is in the “Before restore” safety backup on the Backups page.",
    );
  });

  it("does not guess when a succeeded job's result is not known", () => {
    // a live event marks the cached job as succeeded before its result was read
    const unknown = outcomeMessage(job({ result: {} }));
    expect(unknown).toEqual({
      tone: "success",
      title: "The world was reset",
      description: "The job log on the Activity tab says what was deleted and kept.",
    });
  });

  it("warns about what is left of the old world", () => {
    const left = outcomeMessage(job({ result: { ...done, leftover: ".mcpanel-world-old-ab12", warnings: ["could not delete"] } }));
    expect(left?.tone).toBe("warning");
    expect(left?.description).toMatch(/remove the folder \.mcpanel-world-old-ab12 on the Files page/);
    const warned = outcomeMessage(job({ result: { ...done, warnings: ["a", "b"] } }));
    expect(warned?.tone).toBe("warning");
    expect(warned?.description).toMatch(/The job log has 2 warnings\./);
    expect(outcomeMessage(job({ result: { ...done, deleted: [] } }))).toMatchObject({
      title: "Reset world finished",
      description: expect.stringMatching(/^There was no world folder left to delete\./),
    });
  });

  it("says whether a failed reset touched the world", () => {
    const untouched = outcomeMessage(job({ status: "failed", error: "The world was not changed. The safety backup failed: disk full" }));
    expect(untouched).toEqual({
      tone: "error",
      title: "The world was not reset",
      description: "The world was not changed. The safety backup failed: disk full",
    });
    const notStarted = outcomeMessage(
      job({ status: "failed", error: "The world was reset, but the server could not be started again: port in use" }),
    );
    expect(notStarted?.tone).toBe("warning");
    expect(notStarted?.title).toBe("The world was reset, but the server is not running");
    // a worker that does not know the job never ran it
    expect(outcomeMessage(job({ status: "failed", error: "This worker does not know how to run 'world_reset' jobs.", error_code: "unknown_job_kind" }))?.title).toBe(
      "The world was not reset",
    );
  });

  it("asks for a check when the result of a failed reset is unknown", () => {
    for (const [error, code] of [
      ["Resetting the world failed and the world could not be put back completely. It is kept in the folder '.mcpanel-world-old-ab12'.", "world_rollback_failed"],
      ["The node agent did not answer", "agent_unavailable"],
      ["Marked as failed by colton", "abandoned"],
      ["", "internal_error"],
    ]) {
      const outcome = outcomeMessage(job({ status: "failed", error, error_code: code }));
      expect(outcome?.tone, code).toBe("error");
      expect(outcome?.title).toBe("Reset world failed");
      expect(outcome?.description).toMatch(/The reset may be incomplete\. Check the job log and the world folder before starting the server/);
      if (error) expect(outcome?.description?.startsWith(`${error.replace(/\.$/, "")}. `)).toBe(true);
    }
    expect(outcomeMessage(job({ status: "cancelled" }))).toMatchObject({ tone: "warning", title: "Reset world was cancelled" });
  });
});
