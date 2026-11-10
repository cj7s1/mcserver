/**
 * Pure logic of the "Reset world" dialog (GET/POST /instances/{id}/world/reset): form defaults,
 * seed validation, the request body and the texts derived from a plan or a finished job.
 */
import { formatBytes } from "@/lib/format";
import type { Job, WorldResetFolder, WorldResetKeep, WorldResetPlan, WorldResetSeedMode } from "@/lib/types";

/** Carry-overs in the order they are offered (backend apps/instances/world.py RESET_KEEP). */
export const KEEP_OPTIONS: readonly WorldResetKeep[] = ["datapacks", "config"];

/** Longest custom seed the backend accepts, counted after trimming (WorldResetSerializer). */
export const SEED_MAX_LENGTH = 200;

/**
 * POST errors that mean the plan the user confirmed no longer holds: the folders changed, the server
 * was started, SEED was set, or the plan now has a blocker (every code of backend reset_plan()'s
 * `blockers`). The plan is read again and the options are shown again, where the new state is visible;
 * other errors leave the confirmation open.
 */
export const PLAN_OUTDATED_CODES: readonly string[] = [
  "world_changed",
  "must_stop",
  "seed_env_set",
  "invalid_level_name",
  "level_name_conflict",
  "world_location_overridden",
  "not_a_directory",
  "not_a_world",
  "no_world",
  "agent_outdated",
];

export interface ResetForm {
  seedMode: WorldResetSeedMode;
  /** the custom seed as typed; trimmed when it is sent */
  seed: string;
  /** what the user ticked; options the plan does not offer are never sent */
  keep: Record<WorldResetKeep, boolean>;
  /** take the safety backup first */
  backup: boolean;
}

/** Body of POST /instances/{id}/world/reset without `confirm_name`, which the typed confirmation adds. */
export interface WorldResetBody {
  folders: string[];
  stop_if_running: boolean;
  pre_restore_backup: boolean;
  seed_mode: WorldResetSeedMode;
  seed?: string;
  keep: WorldResetKeep[];
}

/** `result` of a succeeded world_reset job (backend apps/instances/handlers.py). */
export interface WorldResetResult {
  level_name?: string;
  layout?: string;
  deleted?: string[];
  kept?: string[];
  skipped?: string[];
  seed_mode?: WorldResetSeedMode;
  seed_changed?: boolean;
  /** a `.mcpanel-world-old-…` folder that could not be deleted completely */
  leftover?: string | null;
  warnings?: string[];
  safety_backup?: string | null;
  restarted?: boolean;
}

type SeedInfo = Pick<WorldResetPlan, "seed">;

/**
 * The name the safety backup of a reset has in the list on the Backups page (the backend gives
 * it: apps/instances/handlers.py, world_reset). The dialog and the result message name the backup
 * the same way, so it can be found there.
 */
export function safetyBackupName(levelName: string): string {
  return `Before resetting the world '${levelName}'`;
}

/** A seed is configured (SEED in the environment or a non-empty level-seed). */
export function seedIsSet(plan: SeedInfo): boolean {
  return plan.seed.source !== "unset" && plan.seed.value !== "";
}

/** The SEED environment variable decides the seed: the runtime writes it on every start, so it cannot be chosen here. */
export function seedFromEnv(plan: SeedInfo): boolean {
  return plan.seed.source === "env";
}

/** The folders a reset deletes, in the plan's order, with their details when the plan has them. */
export function deletedFolders(plan: Pick<WorldResetPlan, "folders" | "delete">): { path: string; info: WorldResetFolder | null }[] {
  return plan.delete.map((path) => ({ path, info: plan.folders.find((f) => f.path === path) ?? null }));
}

/** Carry-overs that exist in the folders being deleted, with the paths each one moves into the new world. */
export function keepChoices(plan: Pick<WorldResetPlan, "folders" | "delete">): { option: WorldResetKeep; paths: string[] }[] {
  return KEEP_OPTIONS.map((option) => ({
    option,
    paths: deletedFolders(plan).flatMap(({ path, info }) => (info?.keepable?.[option] ?? []).map((name) => `${path}/${name}`)),
  })).filter((choice) => choice.paths.length > 0);
}

/** Defaults: keep a configured seed (otherwise a random one), carry over everything, backup as the panel setting says. */
export function initialForm(plan: Pick<WorldResetPlan, "seed" | "pre_restore_backup_default">): ResetForm {
  return {
    seedMode: seedIsSet(plan) ? "keep" : "random",
    seed: "",
    keep: { datapacks: true, config: true },
    backup: plan.pre_restore_backup_default,
  };
}

/**
 * The form as it applies to `plan`: with SEED in the environment only "keep" is possible, and "keep"
 * is not offered when there is no seed to keep (the plan can change under an open dialog).
 */
export function resolveForm(plan: SeedInfo, form: ResetForm): ResetForm {
  const seedMode = seedFromEnv(plan) ? "keep" : form.seedMode === "keep" && !seedIsSet(plan) ? "random" : form.seedMode;
  return seedMode === form.seedMode ? form : { ...form, seedMode };
}

/**
 * Why the custom seed cannot be used. The backend's rules (apps/instances/world.py validate_seed):
 * 1-200 characters after trimming, no control characters and no line or paragraph separators.
 */
export function seedError(form: Pick<ResetForm, "seedMode" | "seed">): string | null {
  if (form.seedMode !== "custom") return null;
  const seed = form.seed.trim();
  if (!seed) return "Enter a seed, or choose another option.";
  const length = [...seed].length;
  if (length > SEED_MAX_LENGTH) return `A seed can have at most ${SEED_MAX_LENGTH} characters; this one has ${length}.`;
  if (/[\p{Cc}\p{Zl}\p{Zp}]/u.test(seed)) return "A seed cannot contain line breaks, tabs or other control characters.";
  return null;
}

/** The request for what the dialog shows: the folders and the running state come from the plan the user saw. */
export function buildResetBody(plan: Pick<WorldResetPlan, "folders" | "delete" | "running" | "seed">, form: ResetForm): WorldResetBody {
  const f = resolveForm(plan, form);
  return {
    folders: [...plan.delete],
    stop_if_running: plan.running,
    pre_restore_backup: f.backup,
    seed_mode: f.seedMode,
    ...(f.seedMode === "custom" ? { seed: f.seed.trim() } : {}),
    keep: keepChoices(plan)
      .filter((choice) => f.keep[choice.option])
      .map((choice) => choice.option),
  };
}

/** Text of a summary line; `code` parts are folder names, property names and values (shown in monospace). */
export type SummaryPart = string | { code: string };

export interface SummaryLine {
  id: "stop" | "backup" | "delete" | "seed" | "start";
  parts: SummaryPart[];
  /** the step that makes the reset irreversible */
  danger?: boolean;
}

export function summaryText(line: Pick<SummaryLine, "parts">): string {
  return line.parts.map((part) => (typeof part === "string" ? part : part.code)).join("");
}

/** "a", "a and b", "a, b and c" with every item as a code part. */
function codeList(items: readonly string[]): SummaryPart[] {
  return items.flatMap((item, i): SummaryPart[] => [
    ...(i === 0 ? [] : [i === items.length - 1 ? " and " : ", "]),
    { code: item },
  ]);
}

function seedLine(plan: SeedInfo, form: ResetForm): SummaryPart[] {
  const current = plan.seed.value;
  if (seedFromEnv(plan)) {
    return current
      ? ["The seed stays as the ", { code: "SEED" }, " environment variable sets it (", { code: current }, "): the new world is generated from it."]
      : ["The ", { code: "SEED" }, " environment variable is set but empty: the new world gets a random seed."];
  }
  if (form.seedMode === "custom") {
    return [{ code: "level-seed" }, " is set to ", { code: form.seed.trim() }, ": the new world is generated from this seed."];
  }
  if (form.seedMode === "keep") {
    return ["The seed setting is kept (", { code: `level-seed: ${current}` }, "): the new world is generated from this seed."];
  }
  return seedIsSet(plan)
    ? ["The seed setting is cleared (", { code: "level-seed" }, " was ", { code: current }, "): the new world gets a random seed."]
    : ["The new world gets a random seed."];
}

/** What happens, in order, when the user confirms: the list of the confirmation step. */
export function summaryLines(
  plan: Pick<WorldResetPlan, "folders" | "delete" | "running" | "seed" | "level_name">,
  form: ResetForm,
): SummaryLine[] {
  const f = resolveForm(plan, form);
  const deleted = deletedFolders(plan);
  const sized = deleted.every((d) => d.info);
  const total = deleted.reduce((sum, d) => sum + (d.info?.bytes ?? 0), 0);
  const size = !sized ? "" : deleted.length > 1 ? ` (${formatBytes(total)} in total)` : ` (${formatBytes(total)})`;
  const choices = keepChoices(plan);
  const carried = choices.filter((choice) => f.keep[choice.option]).flatMap((choice) => choice.paths);
  return [
    {
      id: "stop",
      parts: [
        !plan.running
          ? "The server stays stopped meanwhile."
          : f.backup
            ? "The server is stopped (players are disconnected) and stays offline during the backup."
            : "The server is stopped (players are disconnected).",
      ],
    },
    f.backup
      ? {
          id: "backup",
          parts: [
            `A safety backup of the server folder is taken and checked (“${safetyBackupName(plan.level_name)}” on the Backups page); like every backup it leaves out old logs, crash reports and caches. If it fails or does not contain the world, nothing is deleted.`,
          ],
        }
      : { id: "backup", parts: ["No safety backup is taken: the world cannot be recovered afterwards."], danger: true },
    {
      id: "delete",
      parts: [
        deleted.length > 1 ? "The world folders " : "The world folder ",
        ...codeList(deleted.map((d) => d.path)),
        `${size} ${deleted.length > 1 ? "are" : "is"} deleted, with all player data in ${deleted.length > 1 ? "them" : "it"}.`,
        ...(carried.length
          ? [" Carried over into the new world: ", ...codeList(carried), "."]
          : choices.length
            ? [" Nothing is carried over."]
            : []),
      ],
    },
    { id: "seed", parts: seedLine(plan, f) },
    {
      id: "start",
      parts: [
        plan.running
          ? "The server is started again and generates the new world."
          : "The server is left stopped. It generates the new world the next time it starts.",
      ],
    },
  ];
}

export interface ResetOutcome {
  tone: "success" | "warning" | "error";
  title: string;
  description?: string;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * What to tell the user once a world_reset job is over; null while it runs (or for another kind of job).
 * A failed job says in its error whether the world was touched (backend handler: "The world was not
 * changed. …", or "The world was reset, but the server could not be started again: …"); any other
 * failure leaves that open, and the server stopped.
 */
export function outcomeMessage(job: Pick<Job, "kind" | "status" | "error" | "error_code" | "result">): ResetOutcome | null {
  if (job.kind !== "world_reset") return null;
  if (job.status === "cancelled") {
    return {
      tone: "warning",
      title: "Reset world was cancelled",
      description: "Check the job log to see how far it got. The server may have been left stopped.",
    };
  }
  if (job.status === "failed") {
    const error = (job.error ?? "").trim();
    // unknown_job_kind: the worker is older than the API and never ran the job
    if (error.startsWith("The world was not changed") || job.error_code === "unknown_job_kind") {
      return { tone: "error", title: "The world was not reset", description: error };
    }
    if (error.startsWith("The world was reset")) {
      return { tone: "warning", title: "The world was reset, but the server is not running", description: error };
    }
    return {
      tone: "error",
      title: "Reset world failed",
      description:
        `${error ? `${error.replace(/[.\s]+$/, "")}. ` : ""}The reset may be incomplete. Check the job log and the world folder before ` +
        "starting the server: a .mcpanel-world-old-… folder may still hold the old world.",
    };
  }
  if (job.status !== "succeeded") return null;
  const result = (job.result ?? {}) as WorldResetResult;
  if (!Array.isArray(result.deleted)) {
    // succeeded, but its result is not known here (a live event carries the status only)
    return { tone: "success", title: "The world was reset", description: "The job log on the Activity tab says what was deleted and kept." };
  }
  const deleted = result.deleted;
  const warnings = result.warnings ?? [];
  const notes = [
    deleted.length ? "" : "There was no world folder left to delete.",
    result.restarted ? "The server was started again and is generating a new world." : "The server generates a new world the next time it starts.",
    result.kept?.length ? `Carried over: ${result.kept.join(", ")}.` : "",
    result.safety_backup
      ? result.level_name
        ? `The old world is in the safety backup “${safetyBackupName(result.level_name)}” on the Backups page.`
        : "The old world is in the safety backup of this reset on the Backups page."
      : "",
    result.leftover
      ? `The old world could not be deleted completely: remove the folder ${result.leftover} on the Files page.`
      : warnings.length
        ? `The job log has ${plural(warnings.length, "warning")}.`
        : "",
  ].filter(Boolean);
  return {
    tone: result.leftover || warnings.length ? "warning" : "success",
    title: deleted.length ? "The world was reset" : "Reset world finished",
    description: notes.join(" "),
  };
}
