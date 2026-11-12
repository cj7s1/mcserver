/** Request bodies of the saves dialogs (SavesDialogs.tsx). */
import { valuesForCreate } from "@/lib/game-config";
import type { ConfigField, ConfigValue } from "@/lib/types";

/** The seed field when the game does not describe its reset form. */
export const DEFAULT_RESET_FIELDS: ConfigField[] = [
  { key: "seed", type: "int", label: "Map seed", nullable: true, help: "Leave empty for a random map.", min: 0, max: 4294967295 },
];

/**
 * The field errors of a refused reset (400 `validation_error`, `details.fields`) that belong to a field of the
 * form: `seed` on its own (outside the game's bound, E-3), the other fields also under `settings`.
 */
export function resetFieldErrors(fields: readonly ConfigField[], details: Record<string, unknown> | undefined): Record<string, string> {
  const raw = (details?.fields ?? {}) as Record<string, unknown>;
  const nested = raw.settings && typeof raw.settings === "object" && !Array.isArray(raw.settings) ? (raw.settings as Record<string, unknown>) : {};
  const text = (v: unknown) => (Array.isArray(v) ? v.map(String).join(" ") : typeof v === "string" ? v : "");
  const out: Record<string, string> = {};
  for (const f of fields) {
    const message = text(f.key === "seed" ? raw.seed : (nested[f.key] ?? raw[f.key]));
    if (message) out[f.key] = message;
  }
  return out;
}

/** Body of POST /saves/reset from the reset form: `seed` on its own, every other changed field in `settings`. */
export function resetBody(
  fields: readonly ConfigField[],
  texts: Record<string, string>,
  confirmName: string,
  backup: boolean,
): { body?: Record<string, unknown>; errors: Record<string, string> } {
  const seedField = fields.find((f) => f.key === "seed");
  const { changes, errors } = valuesForCreate(fields.filter((f) => f.key !== "seed"), texts);
  let seed: ConfigValue = null;
  if (seedField) {
    const raw = (texts.seed ?? "").trim();
    if (raw) {
      if (!/^\d+$/.test(raw)) errors.seed = "Enter a whole number, or leave it empty for a random map";
      else if (seedField.max !== undefined && Number(raw) > seedField.max) errors.seed = `Must be at most ${seedField.max}`;
      else seed = Number(raw);
    }
  }
  if (Object.keys(errors).length) return { errors };
  return {
    body: {
      confirm_name: confirmName,
      seed,
      backup,
      ...(Object.keys(changes).length ? { settings: changes } : {}),
    },
    errors,
  };
}
