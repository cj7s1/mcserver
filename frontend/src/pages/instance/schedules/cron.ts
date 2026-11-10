/**
 * Small, dependency-free helpers for 5-field cron expressions (minute hour day-of-month month day-of-week).
 *
 * The backend (croniter) is authoritative for validation and for computing next runs; this file
 * only gives a friendly description of common patterns and catches obvious typos before saving.
 */

export const CRON_PRESETS: { label: string; cron: string }[] = [
  { label: "Daily 04:00", cron: "0 4 * * *" },
  { label: "Every 6 hours", cron: "0 */6 * * *" },
  { label: "Weekly Sunday 03:00", cron: "0 3 * * 0" },
  { label: "Hourly", cron: "0 * * * *" },
];

const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const DAY_ALIASES: Record<string, number> = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };
const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];
const MONTH_ALIASES: Record<string, number> = {
  jan: 1,
  feb: 2,
  mar: 3,
  apr: 4,
  may: 5,
  jun: 6,
  jul: 7,
  aug: 8,
  sep: 9,
  oct: 10,
  nov: 11,
  dec: 12,
};

const FIELD_SPECS = [
  { name: "minute", min: 0, max: 59 },
  { name: "hour", min: 0, max: 23 },
  { name: "day of month", min: 1, max: 31 },
  { name: "month", min: 1, max: 12, aliases: MONTH_ALIASES },
  { name: "day of week", min: 0, max: 7, aliases: DAY_ALIASES },
] as const;

export function normalizeCron(expr: string): string {
  return expr.trim().split(/\s+/).join(" ");
}

function toNumber(token: string, aliases?: Record<string, number>): number | null {
  if (/^\d+$/.test(token)) return Number(token);
  const alias = aliases?.[token.toLowerCase()];
  return alias === undefined ? null : alias;
}

/** Returns an error message for obviously invalid expressions, or null when it looks valid. */
export function cronError(expr: string): string | null {
  const parts = normalizeCron(expr).split(" ").filter(Boolean);
  if (parts.length !== 5) return `Use 5 fields (minute hour day month weekday); found ${parts.length}.`;
  for (let i = 0; i < 5; i++) {
    const spec = FIELD_SPECS[i];
    const aliases = "aliases" in spec ? spec.aliases : undefined;
    for (const item of parts[i].split(",")) {
      const m = /^([^/]+)(?:\/(\d+))?$/.exec(item);
      if (!m) return `Invalid ${spec.name} field “${parts[i]}”.`;
      const [, base, step] = m;
      if (step !== undefined && Number(step) < 1) return `The ${spec.name} step must be at least 1.`;
      if (base === "*") continue;
      const range = base.split("-");
      if (range.length > 2) return `Invalid ${spec.name} range “${base}”.`;
      for (const r of range) {
        const n = toNumber(r, aliases);
        if (n === null) return `Invalid ${spec.name} value “${r}”.`;
        if (n < spec.min || n > spec.max) return `The ${spec.name} must be between ${spec.min} and ${spec.max}.`;
      }
    }
  }
  return impossibleDateError(parts);
}

/** Longest length of each month (February: 29, which leap years have). */
const MONTH_DAYS = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

/**
 * A day of month that none of the listed months has (e.g. 31 February) never occurs: such a
 * schedule would never run. Only checked when the weekday field is "*", since a restricted weekday
 * makes the schedule run on either match.
 */
function impossibleDateError(parts: string[]): string | null {
  const [, , dom, month, dow] = parts;
  if (dow !== "*" && dow !== "?") return null;
  const days = plainValues(dom);
  if (!days?.length) return null;
  const months = month === "*" ? null : plainValues(month, MONTH_ALIASES);
  if (!months?.length) return null;
  const longest = Math.max(...months.map((m) => MONTH_DAYS[m - 1] ?? 31));
  if (days[0] <= longest) return null;
  const names = months.map((m) => MONTH_NAMES[m - 1]).filter(Boolean);
  return `${listJoin(names)} ${names.length === 1 ? "has" : "have"} no day ${days[0]}${days.length > 1 ? " or later" : ""}, so this schedule would never run.`;
}

/** Parse a field into a sorted list of plain values when it only contains numbers/aliases (no steps). */
function plainValues(field: string, aliases?: Record<string, number>): number[] | null {
  const out: number[] = [];
  for (const item of field.split(",")) {
    if (item.includes("/") || item === "*") return null;
    const range = item.split("-");
    if (range.length === 1) {
      const n = toNumber(range[0], aliases);
      if (n === null) return null;
      out.push(n);
    } else if (range.length === 2) {
      const a = toNumber(range[0], aliases);
      const b = toNumber(range[1], aliases);
      if (a === null || b === null || b < a || b - a > 60) return null;
      for (let n = a; n <= b; n++) out.push(n);
    } else return null;
  }
  return [...new Set(out)].sort((x, y) => x - y);
}

const pad = (n: number) => String(n).padStart(2, "0");

function listJoin(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

function describeTime(minute: string, hour: string): string | null {
  const step = /^\*\/(\d+)$/;
  if (minute === "*" && hour === "*") return "every minute";
  const minuteStep = step.exec(minute);
  if (minuteStep && hour === "*") {
    const n = Number(minuteStep[1]);
    return n === 1 ? "every minute" : `every ${n} minutes`;
  }
  const minutes = plainValues(minute);
  if (!minutes) return null;
  const mins = minutes.map((m) => `:${pad(m)}`);
  if (hour === "*") return `every hour at ${listJoin(mins)}`;
  const hourStep = /^(\*|\d+)\/(\d+)$/.exec(hour);
  if (hourStep) {
    const n = Number(hourStep[2]);
    const from = hourStep[1] === "*" ? 0 : Number(hourStep[1]);
    const start = from === 0 ? "" : ` starting at ${pad(from)}${mins[0]}`;
    return `${n === 1 ? "every hour" : `every ${n} hours`} at ${listJoin(mins)}${start}`;
  }
  const hours = plainValues(hour);
  if (!hours) return null;
  if (hours.length * minutes.length > 6) {
    // a contiguous hour range like 9-17 reads better as a window
    const contiguous = hours.every((h, i) => i === 0 || h === hours[i - 1] + 1);
    if (contiguous && minutes.length === 1)
      return `every hour at ${mins[0]} from ${pad(hours[0])}${mins[0]} to ${pad(hours[hours.length - 1])}${mins[0]}`;
    return null;
  }
  const times: string[] = [];
  for (const h of hours) for (const m of minutes) times.push(`${pad(h)}:${pad(m)}`);
  return `at ${listJoin(times)}`;
}

/** [phrase after "on", phrase used when the weekday is the only restriction] */
function describeWeekdays(dow: string): [string, string] | null {
  const days = plainValues(dow, DAY_ALIASES);
  if (!days) return null;
  const set = [...new Set(days.map((d) => d % 7))].sort((a, b) => a - b);
  if (set.length === 7) return ["", ""];
  if (set.join(",") === "1,2,3,4,5") return ["on weekdays (Mon–Fri)", "on weekdays (Mon–Fri)"];
  if (set.join(",") === "0,6") return ["on weekends", "on weekends"];
  const names = listJoin(set.map((d) => DAY_NAMES[d]));
  return [`on ${names}`, `every ${names}`];
}

function ordinal(n: number): string {
  const s = n % 100 >= 11 && n % 100 <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th";
  return `${n}${s}`;
}

/**
 * Human-readable description of common cron patterns, e.g. "Every day at 04:00".
 * Returns null when the pattern is not one we can describe confidently.
 */
export function describeCron(expr: string): string | null {
  if (cronError(expr)) return null;
  const [minute, hour, dom, month, dow] = normalizeCron(expr).split(" ");
  const time = describeTime(minute, hour);
  if (time === null) return null;

  const domAny = dom === "*" || dom === "?";
  const dowAny = dow === "*" || dow === "?";
  let domPart = "";
  if (!domAny) {
    const days = plainValues(dom);
    if (!days) return null;
    domPart = `on the ${listJoin(days.map(ordinal))} of the month`;
  }
  let dowPart: [string, string] = ["", ""];
  if (!dowAny) {
    const d = describeWeekdays(dow);
    if (d === null) return null;
    dowPart = d;
  }
  let monthPart = "";
  if (month !== "*") {
    const months = plainValues(month, MONTH_ALIASES);
    if (!months) return null;
    monthPart = `in ${listJoin(months.map((m) => MONTH_NAMES[m - 1]))}`;
  }

  let words: string[];
  if (time.startsWith("every")) {
    words = [time, domPart, domPart && dowPart[0] ? `or ${dowPart[0]}` : dowPart[0], monthPart];
  } else if (!domPart && !dowPart[0]) {
    words = ["every day", time, monthPart];
  } else if (!domPart) {
    words = [dowPart[1], time, monthPart];
  } else {
    words = [domPart, dowPart[0] ? `or ${dowPart[0]}` : "", monthPart, time];
  }
  const sentence = words.filter(Boolean).join(" ");
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}

/** The UTC offset embedded in an ISO timestamp from the API, e.g. "UTC" or "UTC+01:00". */
export function offsetFromIso(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const m = /(Z|[+-]\d{2}:\d{2})$/.exec(iso);
  if (!m) return null;
  return m[1] === "Z" || m[1] === "+00:00" ? "UTC" : `UTC${m[1]}`;
}

/** The browser's current UTC offset in the same format as offsetFromIso. */
export function browserOffset(date = new Date()): string {
  const total = -date.getTimezoneOffset();
  if (total === 0) return "UTC";
  const sign = total > 0 ? "+" : "-";
  const abs = Math.abs(total);
  return `UTC${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}
