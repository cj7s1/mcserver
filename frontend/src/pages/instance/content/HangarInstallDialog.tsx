/** Version picker for installing a plugin from Hangar (hangar.papermc.io), SHA-256 verified by the backend. */
import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, Download, ExternalLink, Loader2, Search } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { api, errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
  contentPath,
  isRunningLike,
  safeHttpUrl,
  useContentInventory,
  useHangarVersions,
  useInvalidateContent,
  versionTypeClass,
  type ContentFile,
  type HangarDependency,
  type HangarInstallResponse,
  type HangarVersion,
} from "./content-api";
import type { ContentReport, ReportLine } from "./content-report";
import { Notice, ProjectIcon, type ContentEnv } from "./content-ui";

export interface HangarTarget {
  slug: string;
  title: string | null;
  icon_url: string | null;
  description?: string | null;
  url?: string | null;
  author?: string | null;
  installed: boolean;
}

const norm = (s: string | null | undefined) => (s ?? "").trim().toLowerCase();

/**
 * Installed file that looks like the given plugin: recorded Hangar slug, Modrinth project slug/title, or a
 * jar named after it ("ViaVersion-5.1.jar"). A name match, not a guarantee — shown as "found", not "verified".
 */
export function findInstalled(files: ContentFile[], name: string | null | undefined): ContentFile | null {
  const n = norm(name);
  if (!n) return null;
  return (
    files.find((f) => norm(f.hangar?.slug) === n) ??
    files.find((f) => norm(f.project?.slug) === n || norm(f.project?.title) === n) ??
    files.find((f) => {
      const base = norm(f.filename);
      return base.startsWith(n) && /^[-_. ]/.test(base.slice(n.length));
    }) ??
    null
  );
}

function DependencyList({
  deps,
  files,
  onSearch,
}: {
  deps: HangarDependency[];
  files: ContentFile[];
  onSearch: (name: string) => void;
}) {
  const required = deps.filter((d) => d.type === "required");
  const optional = deps.filter((d) => d.type === "optional");
  if (!deps.length) return <p className="text-xs text-muted-foreground">This version declares no plugin dependencies.</p>;
  return (
    <div className="grid gap-2">
      {required.length > 0 && (
        <ul className="grid gap-1.5" aria-label="Required dependencies">
          {required.map((d, i) => {
            const found = findInstalled(files, d.name);
            const external = safeHttpUrl(d.external_url);
            const name = d.name || "Unnamed dependency";
            return (
              <li key={`${d.name ?? i}`} className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/30 px-2 py-1.5 text-xs">
                <span className="min-w-0 flex-1 truncate font-medium">{name}</span>
                {found ? (
                  <span className="inline-flex min-w-0 items-center gap-1 text-emerald-700 dark:text-emerald-300" title={found.filename}>
                    <CheckCircle2 className="size-3.5 shrink-0" aria-hidden />
                    <span className="truncate">found: {found.filename}</span>
                  </span>
                ) : (
                  <>
                    <span className="text-amber-700 dark:text-amber-300">not installed</span>
                    {external ? (
                      <a
                        href={external}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-primary hover:underline"
                        aria-label={`Download page for ${name} (external site, opens in a new tab)`}
                      >
                        External <ExternalLink className="size-3" aria-hidden />
                      </a>
                    ) : (
                      d.name && (
                        <Button size="xs" variant="outline" onClick={() => onSearch(d.name ?? "")}>
                          <Search /> Find on Hangar
                        </Button>
                      )
                    )}
                  </>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {required.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Hangar dependencies are not installed automatically. Install missing ones before starting the server, or it may refuse
          to load this plugin.
        </p>
      )}
      {optional.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Optional: <span className="text-foreground">{optional.map((d) => d.name || "unnamed").join(", ")}</span> — adds extra
          features when present.
        </p>
      )}
    </div>
  );
}

function VersionOption({ v, id }: { v: HangarVersion; id: string }) {
  const external = !v.installable ? safeHttpUrl(v.file?.external_url) : null;
  return (
    <div
      className={cn(
        "flex items-start rounded-md border transition-colors",
        v.installable ? "hover:bg-muted/50 has-data-checked:border-primary/50 has-data-checked:bg-primary/5" : "bg-muted/20",
      )}
    >
      <label
        htmlFor={id}
        className={cn("flex min-w-0 flex-1 items-start gap-2.5 px-2.5 py-2", v.installable ? "cursor-pointer" : "cursor-not-allowed")}
      >
        <RadioGroupItem value={v.name} id={id} className="mt-0.5" disabled={!v.installable} />
        <span className="grid min-w-0 flex-1 gap-0.5">
          <span className="flex min-w-0 items-center gap-1.5">
            <span className={cn("truncate font-mono text-xs font-medium", !v.installable && "text-muted-foreground")}>{v.name}</span>
            <span className={cn("shrink-0 rounded px-1 text-[10px] font-medium uppercase", versionTypeClass(v.version_type))}>
              {v.version_type ?? "release"}
            </span>
          </span>
          <span className="truncate text-xs text-muted-foreground" title={formatDateTime(v.published)}>
            {v.installable ? "" : `${v.reason ?? "Cannot be installed automatically"} · `}
            {relativeTime(v.published)}
            {v.file?.size ? ` · ${formatBytes(v.file.size)}` : ""}
          </span>
        </span>
      </label>
      {external && (
        <a
          href={external}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex shrink-0 items-center gap-1 self-center px-2.5 text-xs text-primary hover:underline"
          aria-label={`External download page for ${v.name} (opens in a new tab)`}
        >
          Site <ExternalLink className="size-3" aria-hidden />
        </a>
      )}
    </div>
  );
}

function reportFromHangar(res: HangarInstallResponse, files: ContentFile[]): ContentReport {
  const lines: ReportLine[] = [
    {
      tone: "ok",
      label: res.installed.filename,
      detail: [res.installed.version, res.installed.size != null ? formatBytes(res.installed.size) : null, "SHA-256 verified"]
        .filter(Boolean)
        .join(" · "),
    },
  ];
  let missing = 0;
  for (const d of res.required_dependencies) {
    const found = findInstalled(files, d.name);
    if (!found) missing++;
    lines.push(
      found
        ? { tone: "ok", label: d.name ?? "dependency", detail: `required dependency, found as ${found.filename}` }
        : { tone: "warn", label: d.name ?? "dependency", detail: "required dependency, not installed — install it from Hangar" },
    );
  }
  return {
    title: `Installed ${res.installed.slug} ${res.installed.version} from Hangar`,
    tone: missing ? "warning" : "success",
    lines,
    missing: [],
    warnings: res.notes,
  };
}

export function HangarInstallDialog({
  env,
  target,
  onOpenChange,
  onSearch,
}: {
  env: ContentEnv;
  target: HangarTarget | null;
  onOpenChange: (open: boolean) => void;
  /** Search Hangar for a dependency (closes this dialog). */
  onSearch: (query: string) => void;
}) {
  const versions = useHangarVersions(env.instance.id, target?.slug ?? null);
  const inventory = useContentInventory(env.instance.id);
  const files = inventory.data?.files ?? [];
  const [picked, setPicked] = useState<{ slug: string; version: string } | null>(null);
  const invalidate = useInvalidateContent(env.instance.id);

  const list = versions.data?.versions ?? [];
  const installable = list.filter((v) => v.installable);
  const fallback = installable.find((v) => v.version_type === "release") ?? installable[0];
  const selectedName = picked && picked.slug === target?.slug ? picked.version : fallback?.name;
  const selected = list.find((v) => v.name === selectedName);
  const title = target?.title ?? target?.slug ?? "";
  const link = safeHttpUrl(target?.url);
  const existing = findInstalled(files, target?.slug) ?? findInstalled(files, target?.title);
  const sameFile = !!selected?.file?.filename && files.some((f) => f.filename.replace(/\.disabled$/, "") === selected.file?.filename);
  const mcVersion = versions.data?.minecraft_version ?? env.minecraftVersion;

  const install = useMutation({
    mutationFn: (body: { slug: string; version: string }) => api.post<HangarInstallResponse>(contentPath(env.instance.id, "hangar/install"), body),
    onSuccess: (res) => {
      invalidate();
      env.notify(`Installed ${res.installed.slug} ${res.installed.version} from Hangar`, isRunningLike(env.instance));
      env.showReport(reportFromHangar(res, files));
      onOpenChange(false);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  return (
    <Dialog open={!!target} onOpenChange={(o) => !install.isPending && onOpenChange(o)}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <div className="flex items-start gap-3 pr-6">
            <ProjectIcon url={target?.icon_url} className="size-11" />
            <div className="min-w-0">
              <DialogTitle className="truncate">Install {title}</DialogTitle>
              <DialogDescription className="line-clamp-2">
                {target?.description || "Choose a version published for this server's Minecraft version."}
              </DialogDescription>
              <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                {target?.author && <span>by {target.author}</span>}
                {link && (
                  <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
                    View on Hangar <ExternalLink className="size-3" aria-hidden />
                  </a>
                )}
              </p>
            </div>
          </div>
        </DialogHeader>

        {versions.isLoading ? (
          <LoadingState label="Loading versions from Hangar…" className="py-8" />
        ) : versions.error ? (
          <ErrorState error={versions.error} title="Cannot load versions" onRetry={() => versions.refetch()} className="py-6" />
        ) : list.length === 0 ? (
          <EmptyState
            title="No compatible version"
            description={`${title} has no Paper build on Hangar for Minecraft ${mcVersion ?? "this version"}.`}
            className="py-8"
          />
        ) : (
          <div className="grid min-w-0 gap-4">
            {existing && (
              <Notice tone="warning">
                Looks installed already as <span className="font-mono break-all text-foreground">{existing.filename}</span>. Remove or
                disable it in the Installed tab before installing another version, otherwise both jars are loaded.
              </Notice>
            )}
            {!existing && target?.installed && (
              <Notice tone="info">The panel's records show {title} was installed from Hangar before.</Notice>
            )}
            {installable.length === 0 && (
              <Notice tone="warning">
                None of these versions can be downloaded automatically. Get the jar from the plugin's site and add it in the Upload tab.
              </Notice>
            )}
            <fieldset className="grid min-w-0 gap-2">
              <legend className="mb-1 text-xs font-medium">
                Version{" "}
                <span className="font-normal text-muted-foreground">
                  · {list.length} for Paper {mcVersion}
                  {installable.length !== list.length ? ` · ${installable.length} installable` : ""}
                </span>
              </legend>
              <RadioGroup
                value={selectedName ?? ""}
                onValueChange={(v) => target && setPicked({ slug: target.slug, version: v })}
                className="max-h-56 gap-1.5 overflow-y-auto pr-1"
                aria-label="Version to install"
              >
                {list.map((v, i) => (
                  <VersionOption key={v.name} v={v} id={`hangar-version-${i}`} />
                ))}
              </RadioGroup>
              {selected && selected.version_type && selected.version_type !== "release" && (
                <p className="text-xs text-amber-700 dark:text-amber-300">
                  This is a {selected.version_type} build and may be unstable. Back up the server first.
                </p>
              )}
            </fieldset>

            {selected && (
              <div className="grid gap-2">
                <p className="text-xs font-medium">Dependencies</p>
                <DependencyList deps={selected.dependencies} files={files} onSearch={onSearch} />
                {selected.file?.filename && (
                  <p className="text-xs text-muted-foreground">
                    Downloads <span className="font-mono break-all text-foreground">{selected.file.filename}</span> to{" "}
                    <span className="font-mono">/{env.directory}</span> and checks it against the SHA-256 hash Hangar publishes.
                    Plugins run with the server's full permissions — only install ones you trust.
                  </p>
                )}
                {sameFile && <p className="text-xs text-destructive">A file with this name is already in /{env.directory}.</p>}
              </div>
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={install.isPending}>
            Cancel
          </Button>
          <Button
            disabled={!target || !selected || !selected.installable || sameFile || install.isPending || !!env.locked}
            onClick={() => target && selected && install.mutate({ slug: target.slug, version: selected.name })}
          >
            {install.isPending ? <Loader2 className="animate-spin" /> : <Download />}
            {install.isPending ? "Installing…" : "Install"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
