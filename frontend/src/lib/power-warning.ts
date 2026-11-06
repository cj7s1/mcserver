/**
 * In-game warnings before a restart/stop (backend apps/instances/power_warnings.py).
 *
 * POST /instances/{id}/power {action, warn_minutes, warn_message}; schedules store the same two
 * keys in their payload. Players are told at T-N min, at the standard marks below N and at T-10 s.
 */

export type WarnableAction = "restart" | "stop";

/** Allowed warning periods in minutes (backend WARN_MINUTES_CHOICES). */
export const WARN_MINUTES = [0, 1, 5, 10, 15] as const;
export type WarnMinutes = (typeof WARN_MINUTES)[number];
export const WARN_MESSAGE_MAX = 100;
const MARKS = [900, 600, 300, 60, 10];

export function isWarnMinutes(v: unknown): v is WarnMinutes {
  return typeof v === "number" && (WARN_MINUTES as readonly number[]).includes(v);
}

export function warnOptionLabel(minutes: number): string {
  if (minutes === 0) return "No warning";
  return `Warn players ${minutes} minute${minutes === 1 ? "" : "s"} before`;
}

/** Seconds before the action at which players get a chat message. */
export function warningMarks(minutes: number): number[] {
  const total = minutes * 60;
  return total > 0 ? MARKS.filter((m) => m <= total) : [];
}

function markLabel(seconds: number): string {
  return seconds >= 60 ? `${Math.round(seconds / 60)} min` : `${seconds} s`;
}

/** e.g. "Players are told 5 min, 1 min and 10 s before." */
export function describeWarning(minutes: number): string {
  const marks = warningMarks(minutes).map(markLabel);
  if (marks.length === 0) return "Players are not warned.";
  const list = marks.length === 1 ? marks[0] : `${marks.slice(0, -1).join(", ")} and ${marks[marks.length - 1]}`;
  return `Players are told in chat ${list} before.`;
}

/** C0/C1 control characters and the Unicode line/paragraph separators. */
function isControl(ch: string): boolean {
  const c = ch.codePointAt(0) ?? 0;
  return c < 0x20 || (c >= 0x7f && c <= 0x9f) || c === 0x2028 || c === 0x2029;
}

/** Same rules as the backend's validate_message; returns an error text or null. */
export function warnMessageError(text: string): string | null {
  const t = text.trim();
  if (!t) return null;
  if (t.length > WARN_MESSAGE_MAX) return `At most ${WARN_MESSAGE_MAX} characters`;
  if (Array.from(t).some(isControl)) return "Use a single line of plain text";
  if (t.includes("§")) return "Formatting codes (§) are not allowed";
  return null;
}

/** "4:05" (m:ss) until `actionAt`; "0:00" once due; null without a time. */
export function countdownText(actionAt: string | null | undefined, now = Date.now()): string | null {
  if (!actionAt) return null;
  const due = Date.parse(actionAt);
  if (Number.isNaN(due)) return null;
  const left = Math.max(0, Math.ceil((due - now) / 1000));
  const m = Math.floor(left / 60);
  const s = left % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Request body for POST /power (warning fields only for restart/stop with a period). */
export function powerBody(action: string, minutes: number, message: string): Record<string, unknown> {
  if ((action !== "restart" && action !== "stop") || !minutes) return { action };
  const m = message.trim();
  return m ? { action, warn_minutes: minutes, warn_message: m } : { action, warn_minutes: minutes };
}
