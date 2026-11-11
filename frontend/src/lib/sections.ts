/**
 * The one list of a server's sections: the tabs of the server page (InstanceLayout), the server
 * pages in the command palette, the Overview quick links and the Settings tabs all read it. A
 * section is shown when the user has its permission (`perm`) and the instance has every capability
 * it `requires` (lib/games.ts; older panels: today's content_kind / is_modpack rules).
 */
import {
  ArchiveRestore,
  Boxes,
  CalendarClock,
  Cpu,
  DatabaseBackup,
  FileCog,
  FolderOpen,
  History,
  Info,
  Network,
  Package,
  Plug,
  Puzzle,
  Settings,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  SquareTerminal,
  Terminal,
  TriangleAlert,
  Users,
  Variable,
  type LucideIcon,
} from "lucide-react";
import { hasCapabilities, hasCapability } from "./games";
import { hasPerm } from "./queries";
import type { GameCapability, Instance, PermissionCode } from "./types";

export interface InstanceSection {
  key: string;
  /** Path below /servers/<id>; "" is the overview. */
  path: string;
  label: (i: Instance) => string;
  icon: (i: Instance) => LucideIcon;
  perm?: PermissionCode;
  requires?: readonly GameCapability[];
  /** Command palette search words. */
  aliases: readonly string[];
  /** The palette shows it for the current server before the user types. */
  primary?: boolean;
}

const contentLabel = (i: Instance) => (i.content_kind === "plugins" ? "Plugins" : "Mods");

export const INSTANCE_SECTIONS: readonly InstanceSection[] = [
  { key: "overview", path: "", label: () => "Overview", icon: () => Info, aliases: ["status", "summary", "diagnostics"], primary: true },
  {
    key: "console",
    path: "console",
    label: () => "Console",
    icon: () => Terminal,
    perm: "instance.console",
    aliases: ["terminal", "logs", "command", "rcon"],
    primary: true,
  },
  {
    key: "files",
    path: "files",
    label: () => "Files",
    icon: () => FolderOpen,
    perm: "instance.files.read",
    aliases: ["file manager", "config", "upload"],
    primary: true,
  },
  {
    key: "content",
    path: "content",
    label: contentLabel,
    icon: (i) => (i.content_kind === "plugins" ? Plug : Puzzle),
    requires: ["content"],
    aliases: ["mods", "plugins", "content", "modrinth", "hangar"],
    primary: true,
  },
  {
    key: "modpack",
    path: "modpack",
    label: () => "Modpack",
    icon: () => Package,
    requires: ["modpacks"],
    aliases: ["update", "rollback", "pack"],
  },
  {
    key: "players",
    path: "players",
    label: () => "Players",
    icon: () => Users,
    requires: ["players"],
    aliases: ["whitelist", "operators", "ops", "bans", "kick"],
  },
  {
    key: "backups",
    path: "backups",
    label: () => "Backups",
    icon: () => DatabaseBackup,
    perm: "instance.backup",
    aliases: ["restore", "snapshots", "restic"],
    primary: true,
  },
  { key: "schedules", path: "schedules", label: () => "Schedules", icon: () => CalendarClock, aliases: ["cron", "automation", "timer"] },
  { key: "network", path: "network", label: () => "Network", icon: () => Network, aliases: ["ports", "address", "connect", "ip"] },
  {
    key: "settings",
    path: "settings",
    label: () => "Settings",
    icon: () => Settings,
    aliases: ["properties", "memory", "java", "configure", "software"],
    primary: true,
  },
  {
    key: "access",
    path: "access",
    label: () => "Access",
    icon: () => ShieldCheck,
    perm: "instance.access",
    aliases: ["permissions", "users", "grants"],
  },
  { key: "activity", path: "activity", label: () => "Activity", icon: () => History, aliases: ["jobs", "history", "audit"] },
];

function visible(i: Instance, s: { perm?: PermissionCode; requires?: readonly GameCapability[] }): boolean {
  return (!s.perm || hasPerm(i.permissions, s.perm)) && hasCapabilities(i, s.requires);
}

/** The sections of a server the user may open, in tab order. */
export function instanceSections(i: Instance): InstanceSection[] {
  return INSTANCE_SECTIONS.filter((s) => visible(i, s));
}

export function sectionPath(i: Pick<Instance, "id">, section: Pick<InstanceSection, "path">): string {
  return section.path ? `/servers/${i.id}/${section.path}` : `/servers/${i.id}`;
}

// --- Overview quick links -----------------------------------------------------------------------

export interface OverviewShortcut {
  section: InstanceSection;
  description: string;
  icon: LucideIcon;
}

const SHORTCUTS: readonly { key: string; description: string; icon: LucideIcon }[] = [
  { key: "console", description: "Live log output and commands", icon: SquareTerminal },
  { key: "files", description: "Server folder; rotated logs in logs/", icon: FolderOpen },
  { key: "backups", description: "Snapshots and restore", icon: ArchiveRestore },
  { key: "players", description: "Whitelist, operators and bans", icon: Users },
];

/** Quick links on the Overview: a fixed choice of sections, shown when the section is. */
export function overviewShortcuts(i: Instance): OverviewShortcut[] {
  const shown = new Map(instanceSections(i).map((s) => [s.key, s]));
  return SHORTCUTS.flatMap((s) => {
    const section = shown.get(s.key);
    return section ? [{ section, description: s.description, icon: s.icon }] : [];
  });
}

// --- Settings tabs ------------------------------------------------------------------------------

export type SettingsTabId = "game-settings" | "properties" | "resources" | "software" | "environment" | "general" | "danger";

export interface SettingsTab {
  id: SettingsTabId;
  label: (i: Instance) => string;
  icon: LucideIcon;
  perm?: PermissionCode;
  requires?: readonly GameCapability[];
}

export const SETTINGS_TABS: readonly SettingsTab[] = [
  // a non-Minecraft game's own config files (Minecraft has no config_files capability: not shown there)
  { id: "game-settings", label: () => "Game settings", icon: SlidersHorizontal, requires: ["config_files"] },
  { id: "properties", label: () => "Properties", icon: FileCog, requires: ["properties"] },
  { id: "resources", label: (i) => (hasCapability(i, "jvm") ? "Resources & JVM" : "Resources"), icon: Cpu },
  { id: "software", label: () => "Software", icon: Boxes, requires: ["software_update"] },
  { id: "environment", label: () => "Environment", icon: Variable },
  { id: "general", label: () => "General", icon: Settings2 },
  { id: "danger", label: () => "Danger zone", icon: TriangleAlert },
];

/** The Settings tabs of a server, in order; the first one is the default tab. */
export function settingsTabs(i: Instance): SettingsTab[] {
  return SETTINGS_TABS.filter((t) => visible(i, t));
}

/** The tab Settings opens with (Properties for Minecraft, Game settings for games with config files). */
export function defaultSettingsTab(i: Instance): SettingsTabId {
  return settingsTabs(i)[0]?.id ?? "general";
}
