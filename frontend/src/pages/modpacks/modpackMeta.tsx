/** Small presentational helpers shared by the modpack browser, release picker and wizard. */
import type { Provider } from "@/lib/types";
import { cn } from "@/lib/utils";

export const MODPACK_LOADERS = ["fabric", "forge", "neoforge", "quilt"] as const;

const LOADER_LABELS: Record<string, string> = {
  fabric: "Fabric",
  forge: "Forge",
  neoforge: "NeoForge",
  quilt: "Quilt",
  paper: "Paper",
  purpur: "Purpur",
  folia: "Folia",
  spigot: "Spigot",
  bukkit: "Bukkit",
};

const LOADER_STYLES: Record<string, string> = {
  fabric: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
  forge: "bg-orange-500/10 text-orange-700 dark:text-orange-300",
  neoforge: "bg-red-500/10 text-red-700 dark:text-red-300",
  quilt: "bg-fuchsia-500/10 text-fuchsia-700 dark:text-fuchsia-300",
};

export function loaderLabel(loader: string | null | undefined): string {
  if (!loader) return "—";
  return LOADER_LABELS[loader.toLowerCase()] ?? loader.charAt(0).toUpperCase() + loader.slice(1);
}

export const PROVIDER_LABELS: Record<Provider, string> = {
  modrinth: "Modrinth",
  curseforge: "CurseForge",
  ftb: "Feed The Beast",
};

export function providerLabel(provider: string | null | undefined): string {
  return PROVIDER_LABELS[provider as Provider] ?? provider ?? "—";
}

/** What the "follows" count means for each provider. */
export function followsLabel(provider: Provider): string {
  if (provider === "curseforge") return "thumbs up";
  if (provider === "ftb") return "plays";
  return "followers";
}

export function LoaderBadge({ loader, className }: { loader: string; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center rounded-md px-1.5 text-[11px] font-medium",
        LOADER_STYLES[loader.toLowerCase()] ?? "bg-muted text-muted-foreground",
        className,
      )}
    >
      {loaderLabel(loader)}
    </span>
  );
}

export function LoaderBadges({ loaders, className }: { loaders: string[]; className?: string }) {
  if (!loaders.length) return null;
  return (
    <span className={cn("inline-flex flex-wrap gap-1", className)}>
      {loaders.map((l) => (
        <LoaderBadge key={l} loader={l} />
      ))}
    </span>
  );
}

const TYPE_STYLES: Record<string, string> = {
  release: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
  beta: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
  alpha: "bg-red-500/10 text-red-700 dark:text-red-300",
  archived: "bg-zinc-500/10 text-zinc-600 dark:text-zinc-300",
};

export function VersionTypeBadge({ type, className }: { type: string | null | undefined; className?: string }) {
  const t = (type || "release").toLowerCase();
  return (
    <span
      className={cn(
        "inline-flex h-5 items-center rounded-md px-1.5 text-[11px] font-medium capitalize",
        TYPE_STYLES[t] ?? "bg-muted text-muted-foreground",
        className,
      )}
    >
      {t}
    </span>
  );
}

/** "1.20.1, 1.20 +3" — keeps long version lists compact. */
export function compactVersions(versions: string[], max = 3): string {
  if (!versions.length) return "—";
  const shown = versions.slice(0, max).join(", ");
  return versions.length > max ? `${shown} +${versions.length - max}` : shown;
}

/** Sort Minecraft versions newest first (tolerant of 1.x and 26.x schemes). */
export function sortVersionsDesc(versions: string[]): string[] {
  const key = (v: string) => (v.split(/[-+]/)[0].match(/\d+/g) ?? []).map(Number);
  return [...versions].sort((a, b) => {
    const ka = key(a);
    const kb = key(b);
    for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
      const d = (kb[i] ?? -1) - (ka[i] ?? -1);
      if (d) return d;
    }
    return b.localeCompare(a);
  });
}

/** Turn provider markdown/HTML descriptions into readable plain text (no HTML is ever injected). */
export function plainText(markdown: string | null | undefined): string {
  if (!markdown) return "";
  return markdown
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|h\d|li)>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,6}\s*/gm, "")
    .replace(/(\*\*|__)(.*?)\1/g, "$2")
    .replace(/^\s*[-*+]\s+/gm, "• ")
    .replace(/^\s*>\s?/gm, "")
    .replace(/`{1,3}/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
