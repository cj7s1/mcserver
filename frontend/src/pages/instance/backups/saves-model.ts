/** Request bodies of the saves dialogs (SavesDialogs.tsx). */
import { valuesForCreate } from "@/lib/game-config";
import type { ConfigField, ConfigValue } from "@/lib/types";

/** The seed field when the game does not describe its reset form. */
export const DEFAULT_RESET_FIELDS: ConfigField[] = [
  { key: "seed", type: "int", label: "Map seed", nullable: true, help: "Leave empty for a random map.", min: 0, max: 4294967295 },
];

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
