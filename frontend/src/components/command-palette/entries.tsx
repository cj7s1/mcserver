/**
 * What the command palette can show: navigation entries, every visible server with its pages, and
 * per-server power actions. Pure functions of the user's permissions and the instance list, so the
 * palette never offers something the backend would refuse (the backend still enforces everything).
 */
import {
  CalendarClock,
  DatabaseBackup,
  FolderOpen,
  History,
  Info,
  Network,
  Package,
  Play,
  Plug,
  Puzzle,
  RotateCw,
  Settings,
  ShieldCheck,
  Square,
  Terminal,
  Users,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import { StatusDot, statusLabel } from "@/components/StatusBadge";
import { hasPerm } from "@/lib/queries";
import type { Instance, PermissionCode } from "@/lib/types";
import { isRunningLike, serverTypeLabel, type PowerAction } from "@/pages/library/server-utils";

export interface PaletteNavItem {
  to: string;
  label: string;
  icon: ReactNode;
  perm?: PermissionCode;
}

export interface PaletteNavGroup {
  heading: string;
  items: readonly PaletteNavItem[];
}

export interface PaletteEntry {
  /** Stable id (stored in recents): `nav:<path>` or `server:<id>:<section>`. */
  id: string;
  label: string;
  /** Muted context shown before the label (the server name on server pages). */
  context?: string;
  /** Muted text after the label (status and software for servers). */
  hint?: string;
  /** Searchable text; the first keyword is the label and ranks highest. */
  keywords: string[];
  icon: ReactNode;
  to: string;
}

const NAV_ALIASES: Record<string, string[]> = {
  "/": ["home", "overview", "start page"],
  "/servers": ["library", "instances", "list", "all servers"],
  "/servers/new": ["new server", "add", "wizard", "deploy", "install"],
  "/modpacks": ["browse", "modrinth", "curseforge", "ftb"],
  "/activity": ["jobs", "tasks", "history"],
  "/account": ["password", "profile", "me", "sign in"],
  "/admin/users": ["roles", "permissions", "accounts", "admin"],
  "/admin/audit": ["log", "history", "events", "admin"],
  "/admin/system": ["settings", "configuration", "api keys", "admin"],
};

export function navEntries(groups: readonly PaletteNavGroup[], can: (code: PermissionCode) => boolean): { heading: string; entries: PaletteEntry[] }[] {
  return groups
    .map((g) => ({
      heading: g.heading,
      entries: g.items
        .filter((item) => !item.perm || can(item.perm))
        .map((item) => ({
          id: `nav:${item.to}`,
          label: item.label,
          keywords: [item.label, ...(NAV_ALIASES[item.to] ?? []), g.heading],
          icon: item.icon,
          to: item.to,
        })),
    }))
    .filter((g) => g.entries.length > 0);
}

export interface ServerSection {
  key: string;
  /** Path below /servers/<id>; "" is the overview. */
  path: string;
  label: (i: Instance) => string;
  icon: (i: Instance) => LucideIcon;
  perm?: PermissionCode;
  when?: (i: Instance) => boolean;
  aliases: string[];
  /** Shown for the current server even before the user types. */
  primary?: boolean;
}

/** Same visibility rules as the tabs in src/pages/instance/InstanceLayout.tsx. */
export const SERVER_SECTIONS: readonly ServerSection[] = [
  { key: "overview", path: "", label: () => "Overview", icon: () => Info, aliases: ["status", "summary", "diagnostics"], primary: true },
  { key: "console", path: "console", label: () => "Console", icon: () => Terminal, perm: "instance.console", aliases: ["terminal", "logs", "command", "rcon"], primary: true },
  { key: "files", path: "files", label: () => "Files", icon: () => FolderOpen, perm: "instance.files.read", aliases: ["file manager", "config", "upload"], primary: true },
  {
    key: "content",
    path: "content",
    label: (i) => (i.content_kind === "plugins" ? "Plugins" : "Mods"),
    icon: (i) => (i.content_kind === "plugins" ? Plug : Puzzle),
    when: (i) => i.content_kind === "mods" || i.content_kind === "plugins",
    aliases: ["mods", "plugins", "content", "modrinth", "hangar"],
    primary: true,
  },
  { key: "modpack", path: "modpack", label: () => "Modpack", icon: () => Package, when: (i) => i.is_modpack, aliases: ["update", "rollback", "pack"] },
  { key: "players", path: "players", label: () => "Players", icon: () => Users, aliases: ["whitelist", "operators", "ops", "bans", "kick"] },
  { key: "backups", path: "backups", label: () => "Backups", icon: () => DatabaseBackup, perm: "instance.backup", aliases: ["restore", "snapshots", "restic"], primary: true },
  { key: "schedules", path: "schedules", label: () => "Schedules", icon: () => CalendarClock, aliases: ["cron", "automation", "timer"] },
  { key: "network", path: "network", label: () => "Network", icon: () => Network, aliases: ["ports", "address", "connect", "ip"] },
  { key: "settings", path: "settings", label: () => "Settings", icon: () => Settings, aliases: ["properties", "memory", "java", "configure", "software"], primary: true },
  { key: "access", path: "access", label: () => "Access", icon: () => ShieldCheck, perm: "instance.access", aliases: ["permissions", "users", "grants"] },
  { key: "activity", path: "activity", label: () => "Activity", icon: () => History, aliases: ["jobs", "history", "audit"] },
];

export function serverPath(i: Pick<Instance, "id">, section: Pick<ServerSection, "path">): string {
  return section.path ? `/servers/${i.id}/${section.path}` : `/servers/${i.id}`;
}

export function softwareHint(i: Instance): string {
  if (i.is_modpack) return `${i.modpack_name || "Modpack"} ${i.modpack_version_name}`.trim();
  return `${serverTypeLabel(i.server_type)} ${i.minecraft_version}`.trim();
}

/** The status is shown but not searchable: "stop" should find the Stop actions, not every stopped server. */
function serverKeywords(i: Instance): string[] {
  return [i.name, i.group, serverTypeLabel(i.server_type), i.minecraft_version, i.modpack_name, i.node_name].filter(
    (k): k is string => !!k,
  );
}

/** The server itself (opens its overview). */
export function serverEntry(i: Instance): PaletteEntry {
  const archived = !!i.archived_at;
  return {
    id: `server:${i.id}:overview`,
    label: i.name,
    hint: `${archived ? "Archived" : statusLabel(i.status)} · ${softwareHint(i)}`,
    keywords: [...serverKeywords(i), "server", ...(archived ? ["archived"] : [])],
    icon: <StatusDot status={archived ? "archived" : i.status} className="mx-[3px] motion-reduce:[&_.animate-ping]:hidden" />,
    to: serverPath(i, { path: "" }),
  };
}

/** The pages of one server the user may open (overview included). */
export function serverSectionEntries(i: Instance, opts: { primaryOnly?: boolean } = {}): PaletteEntry[] {
  return SERVER_SECTIONS.filter(
    (s) => (!s.perm || hasPerm(i.permissions, s.perm)) && (!s.when || s.when(i)) && (!opts.primaryOnly || s.primary),
  ).map((s) => {
    const label = s.label(i);
    const Icon = s.icon(i);
    return {
      id: `server:${i.id}:${s.key}`,
      label,
      context: i.name,
      keywords: [label, i.name, ...s.aliases],
      icon: <Icon />,
      to: serverPath(i, s),
    };
  });
}

export interface PowerOption {
  action: PowerAction;
  verb: string;
  icon: LucideIcon;
  /** Stop and restart disconnect players, so the palette always asks first. */
  needsConfirm: boolean;
  /** Set when the action is shown but cannot run right now. */
  disabledReason?: string;
  aliases: string[];
}

/**
 * Power actions offered for a server, with the same rules as the power buttons in the server library
 * and the server header: `instance.control`, not archived, Start when stopped, Restart/Stop when the
 * container is up, nothing while it is stopping, disabled while a job holds the server.
 */
export function powerOptions(i: Instance): PowerOption[] {
  if (!hasPerm(i.permissions, "instance.control") || i.archived_at) return [];
  if (i.status === "stopping") return [];
  const disabledReason = i.active_job ? `Busy: ${i.active_job.title || i.active_job.kind}` : undefined;
  if (isRunningLike(i)) {
    return [
      { action: "restart", verb: "Restart", icon: RotateCw, needsConfirm: true, disabledReason, aliases: ["reboot", "power"] },
      { action: "stop", verb: "Stop", icon: Square, needsConfirm: true, disabledReason, aliases: ["shutdown", "halt", "power"] },
    ];
  }
  return [{ action: "start", verb: "Start", icon: Play, needsConfirm: false, disabledReason, aliases: ["boot", "launch", "run", "power"] }];
}

/** Label and search keywords of a power action ("Stop Vanilla Test", or "Stop server" for the current one). */
export function powerItemText(i: Instance, o: PowerOption, contextual: boolean): { label: string; keywords: string[] } {
  const label = contextual ? `${o.verb} server` : `${o.verb} ${i.name}`;
  return { label, keywords: [label, i.name, o.verb, ...o.aliases] };
}

export interface PowerConfirmCopy {
  title: string;
  description: string;
  confirmLabel: string;
  destructive: boolean;
}

export function powerConfirmCopy(i: Instance, action: Exclude<PowerAction, "start">): PowerConfirmCopy {
  if (action === "stop") {
    return {
      title: `Stop ${i.name}?`,
      description: `Players are disconnected. The server saves the world and shuts down gracefully (up to ${i.stop_timeout} s).`,
      confirmLabel: "Stop server",
      destructive: true,
    };
  }
  return {
    title: `Restart ${i.name}?`,
    description: i.restart_required
      ? "Players are disconnected while the server restarts. The container is recreated to apply changed settings or a newer MCPanel version; world data is kept."
      : "Players are disconnected while the server restarts.",
    confirmLabel: "Restart server",
    destructive: false,
  };
}
