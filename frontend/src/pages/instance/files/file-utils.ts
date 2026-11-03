/** Path and file-type helpers for the instance file manager. */
import type { FileEntry } from "@/lib/types";

/** Matches the backend/agent text edit limit (TEXT_EDIT_LIMIT, 5 MiB). */
export const TEXT_EDIT_LIMIT = 5 * 1024 * 1024;

/** Extensions opened in the text editor by default. */
export const TEXT_EXTENSIONS = new Set([
  "properties",
  "json",
  "json5",
  "toml",
  "yml",
  "yaml",
  "txt",
  "cfg",
  "conf",
  "ini",
  "log",
  "snbt",
  "js",
  "zs",
  "mcmeta",
  "md",
  "csv",
  "xml",
  "sh",
]);

/** Extensions the server-side content search looks at (agent SafeRoot.search). */
export const SEARCHED_EXTENSIONS = [
  ".properties",
  ".json",
  ".json5",
  ".toml",
  ".yml",
  ".yaml",
  ".cfg",
  ".conf",
  ".txt",
  ".ini",
  ".snbt",
  ".js",
  ".zs",
  ".mcmeta",
  ".log",
];

/** Normalise a relative path: no leading/trailing slashes, no "." and ".." never climbs above the root. */
export function normalizePath(path: string): string {
  const out: string[] = [];
  for (const part of path.replace(/\\/g, "/").split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      out.pop();
      continue;
    }
    out.push(part);
  }
  return out.join("/");
}

export function joinPath(...parts: string[]): string {
  return normalizePath(parts.filter(Boolean).join("/"));
}

export function parentPath(path: string): string {
  const parts = normalizePath(path).split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

export function baseName(path: string): string {
  const parts = normalizePath(path).split("/");
  return parts[parts.length - 1] ?? "";
}

export function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(i + 1).toLowerCase() : "";
}

export function isTextEditable(entry: Pick<FileEntry, "name" | "type" | "size">): boolean {
  return entry.type === "file" && entry.size < TEXT_EDIT_LIMIT && TEXT_EXTENSIONS.has(extOf(entry.name));
}

/** A file that is not a known text type but small enough to try opening as text. */
export function canTryAsText(entry: Pick<FileEntry, "type" | "size">): boolean {
  return entry.type === "file" && entry.size < TEXT_EDIT_LIMIT;
}

const TYPE_LABELS: Record<string, string> = {
  properties: "Properties",
  json: "JSON",
  json5: "JSON5",
  toml: "TOML",
  yml: "YAML",
  yaml: "YAML",
  txt: "Text",
  cfg: "Config",
  conf: "Config",
  ini: "INI",
  log: "Log",
  snbt: "SNBT",
  js: "JavaScript",
  zs: "ZenScript",
  mcmeta: "Pack metadata",
  md: "Markdown",
  csv: "CSV",
  xml: "XML",
  sh: "Shell script",
  jar: "Java archive",
  zip: "ZIP archive",
  gz: "Gzip archive",
  dat: "NBT data",
  dat_old: "NBT data (old)",
  mca: "Region file",
  png: "PNG image",
  env: "Environment",
  lock: "Lock file",
};

export function typeLabel(entry: FileEntry): string {
  if (entry.type === "directory") return "Folder";
  if (entry.type === "symlink") return "Symbolic link";
  if (entry.type === "other") return "Special file";
  const ext = extOf(entry.name);
  if (!ext) return "File";
  return TYPE_LABELS[ext] ?? `${ext.toUpperCase()} file`;
}

export type SortKey = "name" | "size" | "modified";
export type SortDir = "asc" | "desc";

/** Directories first, then by the chosen key (name ties broken case-insensitively). */
export function sortEntries(entries: FileEntry[], key: SortKey, dir: SortDir): FileEntry[] {
  const mul = dir === "asc" ? 1 : -1;
  const byName = (a: FileEntry, b: FileEntry) => a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });
  return [...entries].sort((a, b) => {
    const da = a.type === "directory" ? 0 : 1;
    const db = b.type === "directory" ? 0 : 1;
    if (da !== db) return da - db;
    let cmp = key === "size" ? a.size - b.size : key === "modified" ? a.modified - b.modified : 0;
    if (cmp === 0) cmp = byName(a, b);
    return cmp * mul;
  });
}

const WORLD_DIR = /^(world|world_nether|world_the_end)$/;

/** Whether a path points into (or at) a world save folder. */
export function isWorldPath(path: string, levelName: string | undefined, entries?: FileEntry[]): boolean {
  const top = normalizePath(path).split("/")[0];
  if (!top) return false;
  if (WORLD_DIR.test(top) || (levelName && (top === levelName || top === `${levelName}_nether` || top === `${levelName}_the_end`)))
    return true;
  return !!entries?.some((e) => e.type === "file" && e.name === "level.dat");
}

/** A top-level world save folder (world, world_nether, <level-name>, …). */
export function isWorldEntry(entry: FileEntry, levelName: string | undefined): boolean {
  return entry.type === "directory" && !entry.path.includes("/") && isWorldPath(entry.path, levelName);
}

/** UTF-8 byte length without allocating an encoded copy (editor size indicator). */
export function utf8Length(text: string): number {
  let bytes = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c < 0x80) bytes += 1;
    else if (c < 0x800) bytes += 2;
    else if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length) {
      bytes += 4;
      i++;
    } else bytes += 3;
  }
  return bytes;
}

export function countLines(text: string): number {
  let n = 1;
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) n++;
  return n;
}

/** Archives the server can extract in place (backend ARCHIVE_SUFFIXES). */
export function isArchive(entry: Pick<FileEntry, "name" | "type">): boolean {
  return entry.type === "file" && /\.(zip|tar|tar\.gz|tgz|tar\.xz|txz)$/i.test(entry.name);
}
