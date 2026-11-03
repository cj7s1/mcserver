/**
 * Log line handling for the web console: sanitising untrusted output before it
 * reaches xterm, level classification and ANSI colouring.
 *
 * Implemented with a small scanner instead of control-character regexes so that
 * only SGR (colour) sequences survive; cursor movement, screen clears, OSC
 * (titles, hyperlinks, clipboard) and other control codes are dropped.
 */

const ESC = 0x1b;
const CSI = "\x1b[";

export const SGR = {
  reset: `${CSI}0m`,
  dim: `${CSI}2m`,
  red: `${CSI}31m`,
  green: `${CSI}32m`,
  yellow: `${CSI}33m`,
  cyan: `${CSI}36m`,
  gray: `${CSI}90m`,
  dimCyan: `${CSI}2;36m`,
  dimItalic: `${CSI}2;3m`,
};

/** Remove control sequences. With keepSgr, `ESC [ <digits;> m` colour sequences are preserved. */
function scan(input: string, keepSgr: boolean): string {
  let out = "";
  const n = input.length;
  let i = 0;
  while (i < n) {
    const c = input.charCodeAt(i);
    if (c === ESC) {
      const next = input[i + 1];
      if (next === "[") {
        let j = i + 2;
        while (j < n && input.charCodeAt(j) >= 0x30 && input.charCodeAt(j) <= 0x3f) j++;
        while (j < n && input.charCodeAt(j) >= 0x20 && input.charCodeAt(j) <= 0x2f) j++;
        if (j < n && input.charCodeAt(j) >= 0x40 && input.charCodeAt(j) <= 0x7e) {
          if (keepSgr && input[j] === "m" && /^[0-9;]*$/.test(input.slice(i + 2, j))) out += input.slice(i, j + 1);
          i = j + 1;
        } else {
          i = j; // incomplete sequence: drop it
        }
        continue;
      }
      if (next === "]" || next === "P" || next === "_" || next === "^") {
        // OSC / DCS / APC / PM: skip to BEL or ST (ESC \)
        let j = i + 2;
        while (j < n && input.charCodeAt(j) !== 0x07 && !(input.charCodeAt(j) === ESC && input[j + 1] === "\\")) j++;
        i = j >= n ? n : input.charCodeAt(j) === 0x07 ? j + 1 : j + 2;
        continue;
      }
      i += 2; // two-character escape
      continue;
    }
    // keep TAB and printable characters; drop other C0, DEL and C1 controls
    if (c === 0x09 || (c >= 0x20 && c !== 0x7f && !(c >= 0x80 && c <= 0x9f))) out += input[i];
    i++;
  }
  return out;
}

export function sanitize(line: string): string {
  return scan(line, true);
}

export function stripAnsi(line: string): string {
  return scan(line, false);
}

/** Minecraft § formatting codes → ANSI (RCON output and some plugin logs use them). */
const MC_CODES: Record<string, string> = {
  "0": "30",
  "1": "34",
  "2": "32",
  "3": "36",
  "4": "31",
  "5": "35",
  "6": "33",
  "7": "37",
  "8": "90",
  "9": "94",
  a: "92",
  b: "96",
  c: "91",
  d: "95",
  e: "93",
  f: "97",
  l: "1",
  m: "9",
  n: "4",
  o: "3",
  r: "0",
};

const MC_RE = /§([0-9a-fk-or])/gi;

export function hasMcCodes(s: string): boolean {
  return s.includes("§");
}

export function mcToAnsi(s: string): string {
  if (!hasMcCodes(s)) return s;
  return s.replace(MC_RE, (_m, code: string) => {
    const sgr = MC_CODES[code.toLowerCase()];
    return sgr ? `${CSI}${sgr}m` : "";
  }) + SGR.reset;
}

export function stripMc(s: string): string {
  return hasMcCodes(s) ? s.replace(MC_RE, "") : s;
}

export type LogLevel = "error" | "warn" | "init" | "done" | "info";

const ERROR_RE = /\b(ERROR|FATAL|SEVERE)\b|Exception\b|^\s+at [\w$.<>/]+\(|^Caused by: |^\s*\.\.\. \d+ more$/;
const WARN_RE = /\bWARN(ING)?\b/;
const INIT_RE = /^\[init\]/;
const DONE_RE = /Done \(\d/;

export function classify(plain: string): LogLevel {
  if (INIT_RE.test(plain)) return "init";
  if (ERROR_RE.test(plain)) return "error";
  if (WARN_RE.test(plain)) return "warn";
  if (DONE_RE.test(plain)) return "done";
  return "info";
}

const LEVEL_COLOR: Record<LogLevel, string | null> = {
  error: SGR.red,
  warn: SGR.yellow,
  init: SGR.dimCyan,
  done: SGR.green,
  info: null,
};

const TIME_PREFIX = /^\[\d{2}:\d{2}:\d{2}\]/;

/** Colour a sanitised log line. Lines that already carry their own colours are passed through. */
export function colorizeLog(clean: string, level: LogLevel): string {
  if (clean.includes(CSI)) return clean + SGR.reset;
  const text = mcToAnsi(clean);
  const color = LEVEL_COLOR[level];
  const m = level === "init" ? null : TIME_PREFIX.exec(text);
  if (m) {
    const rest = text.slice(m[0].length);
    return `${SGR.gray}${m[0]}${SGR.reset}${color ?? ""}${rest}${SGR.reset}`;
  }
  return color ? `${color}${text}${SGR.reset}` : text;
}

export type EntryKind = "log" | "cmd" | "out" | "err" | "sys";

export interface ConsoleEntry {
  kind: EntryKind;
  /** plain text without colour codes (for filtering, copy and download) */
  plain: string;
  lower: string;
  level: LogLevel;
  /** what is written to the terminal */
  rendered: string;
}

export function makeEntry(kind: EntryKind, raw: string): ConsoleEntry {
  const clean = sanitize(raw);
  const plain = stripMc(stripAnsi(clean));
  let level: LogLevel;
  let rendered: string;
  switch (kind) {
    case "log":
      level = classify(plain);
      rendered = colorizeLog(clean, level);
      break;
    case "cmd":
      level = "info";
      rendered = `${SGR.dim}> ${plain}${SGR.reset}`;
      break;
    case "out":
      level = "info";
      rendered = mcToAnsi(clean) + SGR.reset;
      break;
    case "err":
      level = "error";
      rendered = `${SGR.red}✗ ${plain}${SGR.reset}`;
      break;
    default:
      level = "info";
      rendered = `${SGR.dimItalic}── ${plain} ──${SGR.reset}`;
  }
  return { kind, plain, lower: plain.toLowerCase(), level, rendered };
}

export type LevelFilter = "all" | "warn" | "error";

export interface ConsoleFilter {
  text: string; // lower-cased
  level: LevelFilter;
}

export function isFiltering(f: ConsoleFilter): boolean {
  return !!f.text || f.level !== "all";
}

export function matches(e: ConsoleEntry, f: ConsoleFilter): boolean {
  if (e.kind === "sys") return !isFiltering(f);
  if (f.level === "error" && e.level !== "error") return false;
  if (f.level === "warn" && e.level !== "error" && e.level !== "warn") return false;
  if (f.text && !e.lower.includes(f.text)) return false;
  return true;
}

/** Text form of an entry for copy/download. */
export function entryText(e: ConsoleEntry): string {
  if (e.kind === "cmd") return `> ${e.plain}`;
  if (e.kind === "err") return `! ${e.plain}`;
  if (e.kind === "sys") return `-- ${e.plain} --`;
  return e.plain;
}

/**
 * Merge freshly fetched history (oldest → newest) into the live buffer without
 * duplicating the overlap between the two.
 */
export function mergeHistory(history: string[], current: ConsoleEntry[]): ConsoleEntry[] {
  const hist = history.map((l) => makeEntry("log", l));
  const histPlain = hist.map((e) => e.plain);
  const curLogIdx = current.map((e, i) => (e.kind === "log" ? i : -1)).filter((i) => i >= 0);
  const k = Math.min(5, histPlain.length, curLogIdx.length);
  const note = makeEntry("sys", `loaded ${hist.length.toLocaleString()} lines of container log history`);
  if (k === 0) return [note, ...hist, ...current.filter((e) => e.kind !== "log")];

  // 1) history's newest k lines found in the live buffer → keep live entries after that point
  const tail = histPlain.slice(-k);
  for (let a = curLogIdx.length - k; a >= 0; a--) {
    let ok = true;
    for (let b = 0; b < k; b++) {
      if (current[curLogIdx[a + b]].plain !== tail[b]) {
        ok = false;
        break;
      }
    }
    if (ok) {
      const cut = curLogIdx[a + k - 1];
      return [note, ...hist, ...current.slice(cut + 1)];
    }
  }
  // 2) the live buffer's newest k log lines are inside the history → history already covers them
  const liveTail = curLogIdx.slice(-k).map((i) => current[i].plain);
  for (let a = histPlain.length - k; a >= 0; a--) {
    let ok = true;
    for (let b = 0; b < k; b++) {
      if (histPlain[a + b] !== liveTail[b]) {
        ok = false;
        break;
      }
    }
    if (ok) {
      const lastLive = curLogIdx[curLogIdx.length - 1];
      return [note, ...hist.slice(0, a + k), ...current.slice(lastLive + 1)];
    }
  }
  // no overlap (the log was rotated or the container recreated): history first, then live output
  return [note, ...hist, makeEntry("sys", "live output"), ...current];
}
