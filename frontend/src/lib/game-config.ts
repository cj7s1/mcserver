/**
 * The field schema of a game's config files, options and reset forms (backend contract "Field
 * schema"): turning stored JSON values into form text and back, validation, and the set of changes a
 * form sends. Secrets arrive masked (MASK); a form never sends a masked value back, so an untouched
 * secret stays as it is on the server.
 */
import type { ConfigField, ConfigValue } from "./types";

/** What the API sends instead of a secret value. */
export const MASK = "********";

/** Form text of a stored value (lists one entry per line, null = empty). */
export function toText(field: ConfigField, value: ConfigValue | undefined): string {
  if (value === undefined || value === null) return "";
  if (Array.isArray(value)) return value.join("\n");
  if (field.type === "bool") return value === true || value === "true" ? "true" : "false";
  return String(value);
}

/** The JSON value of form text, or an error message. */
export function fromText(field: ConfigField, text: string): { value: ConfigValue } | { error: string } {
  const t = field.type === "string" || field.type === "list" ? text : text.trim();
  if (field.type === "bool") return { value: t === "true" };
  if (field.type === "list") {
    return { value: text.split("\n").map((l) => l.trim()).filter(Boolean) };
  }
  if (t === "" && (field.nullable || field.type === "int" || field.type === "number")) {
    if (field.nullable) return { value: null };
    return { error: "Enter a number" };
  }
  if (field.type === "int" || field.type === "number") {
    if (field.type === "int" ? !/^-?\d+$/.test(t) : !/^-?\d+(\.\d+)?$/.test(t)) {
      return { error: field.type === "int" ? "Enter a whole number" : "Enter a number" };
    }
    const n = Number(t);
    if (field.min !== undefined && n < field.min) return { error: `Must be at least ${field.min}` };
    if (field.max !== undefined && n > field.max) return { error: `Must be at most ${field.max}` };
    return { value: n };
  }
  if (field.type === "enum") {
    if (field.options && !field.options.includes(t)) return { error: `Choose one of ${field.options.join(", ")}` };
    return { value: t };
  }
  if (text.length > 4096) return { error: "Too long (max 4096 characters)" };
  if (/[\r\n]/.test(text)) return { error: "Must be a single line" };
  const mismatch = patternError(text, field.pattern, field.pattern_help);
  if (mismatch) return { error: mismatch };
  return { value: text };
}

const patternCache = new Map<string, RegExp | null>();

/**
 * A server-side pattern (Python syntax, matched whole with re.fullmatch) as a JS RegExp; null when JS
 * cannot compile it (the server still checks it). The six games' patterns compile the same in both.
 */
export function patternRegExp(pattern: string): RegExp | null {
  let re = patternCache.get(pattern);
  if (re === undefined) {
    re = null;
    for (const flags of ["u", ""]) {
      try {
        re = new RegExp(`^(?:${pattern})$`, flags);
        break;
      } catch {
        /* try without the unicode flag, then give up */
      }
    }
    patternCache.set(pattern, re);
  }
  return re;
}

/** Why `text` does not match `pattern` (`help` says what is allowed), or null. */
export function patternError(text: string, pattern?: string, help?: string): string | null {
  if (!pattern) return null;
  const re = patternRegExp(pattern);
  if (!re || re.test(text)) return null;
  return help ? `Allowed: ${help}` : "Contains characters that are not allowed";
}

/** Form text of every field: the stored value, else the field's default. */
export function initialTexts(fields: readonly ConfigField[], values: Record<string, ConfigValue> = {}): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of fields) out[f.key] = toText(f, f.key in values ? values[f.key] : f.default);
  return out;
}

export interface FormDiff {
  /** Fields that changed, as JSON values (masked secrets that were not touched are left out). */
  changes: Record<string, ConfigValue>;
  errors: Record<string, string>;
}

/**
 * The changes between the texts a form started with and the texts now. A secret whose text is still
 * the mask (or was masked and is now empty because the user cleared nothing) is never sent.
 */
export function diffTexts(fields: readonly ConfigField[], before: Record<string, string>, after: Record<string, string>): FormDiff {
  const changes: Record<string, ConfigValue> = {};
  const errors: Record<string, string> = {};
  for (const f of fields) {
    const now = after[f.key] ?? "";
    const was = before[f.key] ?? "";
    if (now === was) continue;
    if (f.secret && now === MASK) continue;
    const parsed = fromText(f, now);
    if ("error" in parsed) errors[f.key] = parsed.error;
    else changes[f.key] = parsed.value;
  }
  return { changes, errors };
}

/** Values of every field for a create request (only fields that differ from their default are sent). */
export function valuesForCreate(fields: readonly ConfigField[], texts: Record<string, string>): FormDiff {
  return diffTexts(fields, initialTexts(fields), texts);
}

/** Fields grouped by `group` (in first-seen order), ungrouped ones under "". */
export function groupFields(fields: readonly ConfigField[]): [string, ConfigField[]][] {
  const groups = new Map<string, ConfigField[]>();
  for (const f of fields) {
    const g = f.group ?? "";
    groups.set(g, [...(groups.get(g) ?? []), f]);
  }
  return [...groups.entries()];
}

/** A pinned option's value from a server's `game_options` (undefined for a virtual config kept in the same map). */
export function optionValue(options: Record<string, unknown> | null | undefined, key: string): ConfigValue | undefined {
  const value = options?.[key];
  if (value === undefined || (value !== null && typeof value === "object" && !Array.isArray(value))) return undefined;
  return value as ConfigValue;
}

/** The Select value standing for an enum's empty option (Radix Select items cannot have the value ""). */
export const EMPTY_OPTION = "__mcpanel_empty__";

/** What an enum's empty option is called: its label from the schema, else "Default" (the game's own default). */
export function emptyOptionLabel(field: Pick<ConfigField, "option_labels">): string {
  return field.option_labels?.[""] || "Default";
}

/** Display text of a value in summaries ("On", "Off", "Not set", the mask for secrets). */
export function displayValue(field: ConfigField, value: ConfigValue | undefined): string {
  if (field.secret) return value ? "Set" : "Not set";
  if (field.type === "enum" && value === "" && field.options?.includes("")) return emptyOptionLabel(field);
  if (value === undefined || value === null || value === "") return field.nullable ? "Random / not set" : "Not set";
  if (field.type === "bool") return value === true ? "On" : "Off";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "None";
  if (field.option_labels?.[String(value)]) return field.option_labels[String(value)];
  return String(value);
}
