import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import { ErrorState } from "@/components/States";
import { formatBytes, formatMemoryMb } from "@/lib/format";
import type { Provider } from "@/lib/types";
import { cn } from "@/lib/utils";
import { loaderLabel } from "./modpackMeta";
import { useResolveRelease } from "./modpackQueries";

export const RUNTIME_LABELS: Record<string, string> = {
  MODRINTH: "Modrinth pack installer (.mrpack)",
  AUTO_CURSEFORGE: "CurseForge pack installer",
  FTBA: "FTB pack installer",
};

/** Server-side validation of a modpack release: what the panel will install, plus warnings. */
export function ResolvePreview({
  provider,
  projectId,
  versionId,
  className,
}: {
  provider: Provider;
  projectId: string;
  versionId: string;
  className?: string;
}) {
  const q = useResolveRelease(provider, projectId, versionId);
  if (q.isLoading) {
    return (
      <div className={cn("flex items-center gap-2 rounded-lg border bg-muted/30 p-3 text-sm text-muted-foreground", className)} role="status">
        <Loader2 className="size-4 animate-spin" /> Checking this release for server deployment…
      </div>
    );
  }
  if (q.isError) {
    return <ErrorState error={q.error} onRetry={() => q.refetch()} title="This release cannot be deployed" className={cn("py-6", className)} />;
  }
  const r = q.data;
  if (!r) return null;
  const rows: [string, string][] = [
    ["Installer", RUNTIME_LABELS[r.server_type] ?? r.server_type],
    ["Release", r.version_name || r.version_id],
    ["Minecraft", r.minecraft_version],
    ["Mod loader", `${loaderLabel(r.loader)}${r.loader_version ? ` ${r.loader_version}` : " (version from the pack manifest)"}`],
    ["Java", r.java ? `Java ${r.java.java_version} · ${r.java.reason}` : "Auto"],
  ];
  if (r.file?.filename) rows.push(["Pack file", `${r.file.filename}${r.file.size ? ` (${formatBytes(r.file.size)})` : ""}`]);
  if (r.recommended_memory_mb) rows.push(["Memory", `${formatMemoryMb(r.recommended_memory_mb)} recommended by the pack`]);
  return (
    <div className={cn("grid gap-2 rounded-lg border bg-muted/20 p-3", className)} aria-live="polite">
      <div className="flex items-center gap-2 text-sm font-medium">
        <CheckCircle2 className="size-4 text-emerald-500" aria-hidden /> Ready to deploy
      </div>
      <dl className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="min-w-0 break-words">{v}</dd>
          </div>
        ))}
      </dl>
      {r.warnings.length > 0 && (
        <ul className="grid gap-1 rounded-md border border-amber-500/30 bg-amber-500/10 p-2 text-xs text-amber-800 dark:text-amber-200">
          {r.warnings.map((w) => (
            <li key={w} className="flex gap-1.5">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              {w}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
