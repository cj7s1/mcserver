/** Version picker + dependency notice for installing a Modrinth project. */
import { useMutation, useQueries } from "@tanstack/react-query";
import { CheckCircle2, Download, ExternalLink, Loader2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { api, errorMessage } from "@/lib/api";
import { formatBytes, formatNumber, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
  contentKeys,
  contentPath,
  useInvalidateContent,
  useProjectVersions,
  versionTypeClass,
  type ContentVersion,
  type InstallResponse,
  type ProjectVersionsResponse,
} from "./content-api";
import { reportFromInstall } from "./content-report";
import { Notice, ProjectIcon, type ContentEnv } from "./content-ui";

export interface InstallTarget {
  id: string;
  title: string | null;
  icon_url: string | null;
  description?: string | null;
  url?: string | null;
}

function DependencyList({ env, version, withDeps }: { env: ContentEnv; version: ContentVersion; withDeps: boolean }) {
  const required = version.dependencies.filter((d) => d.type === "required");
  const optional = version.dependencies.filter((d) => d.type === "optional").length;
  const incompatible = version.dependencies.filter((d) => d.type === "incompatible").length;
  const lookups = useQueries({
    queries: required.map((d) => ({
      queryKey: contentKeys.versions(env.instance.id, d.project_id ?? ""),
      queryFn: () =>
        api.get<ProjectVersionsResponse>(contentPath(env.instance.id, `projects/${encodeURIComponent(d.project_id ?? "")}/versions`)),
      enabled: !!d.project_id,
      staleTime: 60_000,
      retry: false,
    })),
  });
  if (!required.length && !optional && !incompatible) {
    return <p className="text-xs text-muted-foreground">This version declares no dependencies.</p>;
  }
  return (
    <div className="grid gap-2">
      {required.length > 0 && (
        <ul className="grid gap-1.5" aria-label="Required dependencies">
          {required.map((d, i) => {
            const q = lookups[i];
            const name = q.data?.project.title || d.project_id || `version ${d.version_id}`;
            let state: { text: string; tone: string };
            if (!d.project_id) state = { text: "resolved during install", tone: "text-muted-foreground" };
            else if (q.isLoading) state = { text: "checking…", tone: "text-muted-foreground" };
            else if (q.error) state = { text: errorMessage(q.error), tone: "text-destructive" };
            else if (q.data?.installed) state = { text: "installed", tone: "text-emerald-700 dark:text-emerald-300" };
            else if (q.data && q.data.versions.length === 0)
              state = { text: `no version for ${env.platform} ${env.minecraftVersion ?? ""}`.trim(), tone: "text-destructive" };
            else state = withDeps ? { text: "will be installed", tone: "text-sky-700 dark:text-sky-300" } : { text: "not installed", tone: "text-amber-700 dark:text-amber-300" };
            return (
              <li key={d.project_id ?? d.version_id ?? i} className="flex items-center gap-2 rounded-md border bg-muted/30 px-2 py-1.5 text-xs">
                <ProjectIcon url={q.data?.project.icon_url} className="size-6" />
                <span className="min-w-0 flex-1 truncate font-medium">{name}</span>
                <span className={cn("shrink-0 text-right", state.tone)}>
                  {q.data?.installed && <CheckCircle2 className="mr-1 inline size-3.5" aria-hidden />}
                  {state.text}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {(optional > 0 || incompatible > 0) && (
        <p className="text-xs text-muted-foreground">
          {optional > 0 && `${optional} optional ${optional === 1 ? "dependency is" : "dependencies are"} not installed automatically. `}
          {incompatible > 0 && `Declares ${incompatible} incompatible ${incompatible === 1 ? "project" : "projects"}; you will be warned if one is installed.`}
        </p>
      )}
    </div>
  );
}

function VersionOption({ v, id }: { v: ContentVersion; id: string }) {
  return (
    <label
      htmlFor={id}
      className="flex cursor-pointer items-start gap-2.5 rounded-md border px-2.5 py-2 transition-colors hover:bg-muted/50 has-data-checked:border-primary/50 has-data-checked:bg-primary/5"
    >
      <RadioGroupItem value={v.id} id={id} className="mt-0.5" />
      <span className="grid min-w-0 flex-1 gap-0.5">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="truncate font-mono text-xs font-medium">{v.version_number ?? v.id}</span>
          <span className={cn("shrink-0 rounded px-1 text-[10px] font-medium uppercase", versionTypeClass(v.version_type))}>
            {v.version_type ?? "release"}
          </span>
        </span>
        <span className="truncate text-xs text-muted-foreground">
          {v.name && v.name !== v.version_number ? `${v.name} · ` : ""}
          {relativeTime(v.published)}
          {v.downloads != null ? ` · ${formatNumber(v.downloads)} downloads` : ""}
          {v.file?.size ? ` · ${formatBytes(v.file.size)}` : ""}
        </span>
      </span>
    </label>
  );
}

export function InstallContentDialog({
  env,
  target,
  onOpenChange,
}: {
  env: ContentEnv;
  target: InstallTarget | null;
  onOpenChange: (open: boolean) => void;
}) {
  const versions = useProjectVersions(env.instance.id, target?.id ?? null);
  const [picked, setPicked] = useState<{ project: string; version: string } | null>(null);
  const [withDeps, setWithDeps] = useState(true);
  const invalidate = useInvalidateContent(env.instance.id);

  const list = versions.data?.versions ?? [];
  const fallback = list.find((v) => v.version_type === "release") ?? list[0];
  const selectedId = picked && picked.project === target?.id ? picked.version : fallback?.id;
  const selected = list.find((v) => v.id === selectedId);
  const hasRequired = !!selected?.dependencies.some((d) => d.type === "required");
  const project = versions.data?.project;
  const title = project?.title ?? target?.title ?? target?.id ?? "";
  const link = project?.url ?? target?.url ?? null;

  const install = useMutation({
    mutationFn: (body: { project_id: string; version_id?: string; install_dependencies: boolean }) =>
      api.post<InstallResponse>(contentPath(env.instance.id, "install"), body),
    onSuccess: (res) => {
      invalidate();
      const report = reportFromInstall(res, env.noun);
      env.notify(report.title, res.restart_required);
      env.showReport(report.missing.length || report.warnings.length || res.installed.length > 1 ? report : null);
      onOpenChange(false);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  const already = versions.data?.installed === true;

  return (
    <Dialog open={!!target} onOpenChange={(o) => !install.isPending && onOpenChange(o)}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <div className="flex items-start gap-3 pr-6">
            <ProjectIcon url={project?.icon_url ?? target?.icon_url} className="size-11" />
            <div className="min-w-0">
              <DialogTitle className="truncate">Install {title}</DialogTitle>
              <DialogDescription className="line-clamp-2">
                {project?.description ?? target?.description ?? `Choose a version compatible with this server.`}
              </DialogDescription>
              {link && (
                <a
                  href={link}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-1 inline-flex items-center gap-1 text-xs text-primary hover:underline"
                >
                  View on Modrinth <ExternalLink className="size-3" aria-hidden />
                </a>
              )}
            </div>
          </div>
        </DialogHeader>

        {versions.isLoading ? (
          <LoadingState label="Loading compatible versions…" className="py-8" />
        ) : versions.error ? (
          <ErrorState error={versions.error} title="Cannot install this project" onRetry={() => versions.refetch()} className="py-6" />
        ) : list.length === 0 ? (
          <EmptyState
            title="No compatible version"
            description={`${title} has no version for ${env.platform} on Minecraft ${env.minecraftVersion ?? "this version"}.`}
            className="py-8"
          />
        ) : (
          <div className="grid min-w-0 gap-4">
            {already && (
              <Notice tone="info">
                {title} is already installed. Use the Installed tab to update it or change its version.
              </Notice>
            )}
            <fieldset className="grid min-w-0 gap-2">
              <legend className="mb-1 text-xs font-medium">
                Version{" "}
                <span className="font-normal text-muted-foreground">
                  · {list.length} for {env.platform} {env.minecraftVersion}
                </span>
              </legend>
              <RadioGroup
                value={selectedId}
                onValueChange={(v) => target && setPicked({ project: target.id, version: v })}
                className="max-h-56 gap-1.5 overflow-y-auto pr-1"
                aria-label="Version to install"
              >
                {list.map((v) => (
                  <VersionOption key={v.id} v={v} id={`install-version-${v.id}`} />
                ))}
              </RadioGroup>
              {selected && selected.version_type !== "release" && (
                <p className="text-xs text-amber-700 dark:text-amber-300">
                  This is a {selected.version_type} build and may be unstable. Back up the server first.
                </p>
              )}
            </fieldset>

            {selected && (
              <div className="grid gap-2">
                <p className="text-xs font-medium">Dependencies</p>
                <DependencyList env={env} version={selected} withDeps={withDeps} />
                {hasRequired && (
                  <div className="flex items-start gap-2 pt-1">
                    <Checkbox id="install-with-deps" checked={withDeps} onCheckedChange={(c) => setWithDeps(c === true)} className="mt-0.5" />
                    <div className="grid gap-0.5">
                      <Label htmlFor="install-with-deps">Also install required dependencies</Label>
                      <p className="text-xs text-muted-foreground">
                        Installs the newest compatible stable version of each missing dependency (up to three levels deep).
                      </p>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={install.isPending}>
            Cancel
          </Button>
          <Button
            disabled={!target || !selected || already || install.isPending || !!env.locked}
            onClick={() =>
              target &&
              selected &&
              install.mutate({ project_id: target.id, version_id: selected.id, install_dependencies: hasRequired && withDeps })
            }
          >
            {install.isPending ? <Loader2 className="animate-spin" /> : <Download />}
            {install.isPending ? "Installing…" : "Install"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
