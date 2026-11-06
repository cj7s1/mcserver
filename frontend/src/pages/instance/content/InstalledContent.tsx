/** Installed mods/plugins: table with enable switches, removal, update checks and bulk updates. */
import { useMutation } from "@tanstack/react-query";
import { ArrowUpCircle, ExternalLink, Loader2, PackageSearch, RefreshCw, Search, Trash2, Upload } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { ConfirmAction } from "./ConfirmAction";
import { EmptyState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { api, errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
  contentPath,
  displayTitle,
  modrinthUrl,
  safeHttpUrl,
  useContentUpdates,
  useInvalidateContent,
  versionTypeClass,
  type ContentFile,
  type ContentInventory,
  type ContentUpdate,
  type DeleteResponse,
  type ToggleResponse,
  type UpdateResponse,
} from "./content-api";
import { reportFromUpdate } from "./content-report";
import { Notice, ProjectIcon, type ContentEnv } from "./content-ui";

type StatusFilter = "all" | "enabled" | "disabled" | "updates" | "manual";
type SortKey = "name" | "size" | "modified";

function useUpdateFiles(env: ContentEnv, onDone?: () => void) {
  const invalidate = useInvalidateContent(env.instance.id);
  return useMutation({
    mutationFn: (files: string[]) => api.post<UpdateResponse>(contentPath(env.instance.id, "update"), { files }),
    onSuccess: (res) => {
      invalidate();
      onDone?.();
      const report = reportFromUpdate(res);
      if (res.updated > 0) env.notify(report.title, res.restart_required);
      else if (res.failed > 0) toast.error(report.title);
      else toast.success(report.title);
      const interesting = res.failed > 0 || res.missing_dependencies.length > 0 || report.warnings.length > 0 || res.results.length > 1;
      env.showReport(interesting ? report : null);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
}

function BulkUpdateConfirm({
  env,
  count,
  pending,
  label,
  onConfirm,
}: {
  env: ContentEnv;
  count: number;
  pending: boolean;
  label: string;
  onConfirm: () => void;
}) {
  return (
    <ConfirmAction
      trigger={
        <Button size="sm" disabled={!!env.locked || pending}>
          {pending ? <Loader2 className="animate-spin" /> : <ArrowUpCircle />} {label}
        </Button>
      }
      title={`Update ${count} ${count === 1 ? env.noun.one : env.noun.many}?`}
      description={
        <div className="grid gap-2">
          <p>
            Each file is replaced by its newest compatible {env.platform} {env.minecraftVersion} version from Modrinth and the old jar is
            deleted. Files that are already up to date or not on Modrinth are skipped.
          </p>
          {env.instance.is_modpack && (
            <p className="text-amber-700 dark:text-amber-300">
              This server runs a modpack. Newer versions of the pack's {env.noun.many} can conflict with its configs and other{" "}
              {env.noun.many}; updating the modpack itself is usually safer.
            </p>
          )}
          <p>Take a backup first if you may want to roll back.</p>
        </div>
      }
      confirmLabel="Update"
      onConfirm={onConfirm}
    />
  );
}

function SourceBadges({ file, env }: { file: ContentFile; env: ContentEnv }) {
  const badges: { label: string; tip: string; className: string }[] = [];
  if (file.source === "manual")
    badges.push({
      label: "Manual",
      tip: `Not found on Modrinth (added by hand or downloaded elsewhere). The panel cannot check it for updates or compatibility.`,
      className: "bg-muted text-muted-foreground",
    });
  if (file.source === "unknown")
    badges.push({
      label: "Unidentified",
      tip: "Modrinth could not be reached to identify this file.",
      className: "bg-muted text-muted-foreground",
    });
  if (file.source === "hangar")
    badges.push({
      label: "Hangar",
      tip: `Installed from Hangar${file.hangar?.version ? ` (version ${file.hangar.version})` : ""}. Hangar has no update check here; look for newer versions in the Hangar tab.`,
      className: "bg-sky-500/10 text-sky-700 dark:text-sky-300",
    });
  if (file.source === "spigot")
    badges.push({
      label: "SpigotMC",
      tip: `Installed from SpigotMC via Spiget${file.spigot?.version ? ` (version ${file.spigot.version})` : ""}. Open it in the SpigotMC tab to check Spiget's copy for an update.`,
      className: "bg-orange-500/10 text-orange-700 dark:text-orange-300",
    });
  if (file.compatibility && !file.compatibility.loader)
    badges.push({
      label: `Not for ${env.platform}`,
      tip: `Modrinth lists this version for ${file.version?.loaders.join(", ") || "another platform"}; this ${env.platform} server will probably not load it.`,
      className: "bg-destructive/10 text-destructive",
    });
  if (file.compatibility && file.compatibility.minecraft === false)
    badges.push({
      label: "Other MC version",
      tip: `This version is published for Minecraft ${file.version?.game_versions.slice(-3).join(", ") || "other versions"}, not ${env.minecraftVersion}.`,
      className: "bg-amber-500/10 text-amber-700 dark:text-amber-300",
    });
  if (!badges.length) return null;
  return (
    <>
      {badges.map((b) => (
        <Tooltip key={b.label}>
          <TooltipTrigger asChild>
            <span tabIndex={0} className={cn("inline-flex h-5 items-center rounded-full px-1.5 text-[11px] font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring", b.className)}>
              {b.label}
            </span>
          </TooltipTrigger>
          <TooltipContent>{b.tip}</TooltipContent>
        </Tooltip>
      ))}
    </>
  );
}

function InstalledRow({
  file,
  env,
  update,
  selected,
  onSelect,
  updating,
  onUpdate,
}: {
  file: ContentFile;
  env: ContentEnv;
  update: ContentUpdate | undefined;
  selected: boolean;
  onSelect: (checked: boolean) => void;
  updating: boolean;
  onUpdate: () => void;
}) {
  const invalidate = useInvalidateContent(env.instance.id);
  const title = displayTitle(file);
  const toggle = useMutation({
    mutationFn: (enabled: boolean) =>
      api.post<ToggleResponse>(contentPath(env.instance.id, "toggle"), { filename: file.filename, enabled }),
    onSuccess: (res) => {
      invalidate();
      if (res.changed) env.notify(`${res.enabled ? "Enabled" : "Disabled"} ${title}`, res.restart_required);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const remove = useMutation({
    mutationFn: () => api.delete<DeleteResponse>(contentPath(env.instance.id, "files"), undefined, { filename: file.filename }),
    onSuccess: (res) => {
      invalidate();
      env.notify(`Removed ${res.deleted}`, res.restart_required);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const mutable = env.canEdit && !env.locked;
  const link = file.source === "spigot" ? safeHttpUrl(file.spigot?.url) : modrinthUrl(file.project);
  const enabled = toggle.isPending ? !!toggle.variables : file.enabled;

  const versionEl = (
    <div className="flex flex-col items-start gap-1">
      {file.version ? (
        <span className="flex items-center gap-1.5">
          <span className="max-w-40 truncate font-mono text-xs" title={file.version.name ?? undefined}>
            {file.version.version_number ?? "—"}
          </span>
          {file.version.version_type && file.version.version_type !== "release" && (
            <span className={cn("rounded px-1 text-[10px] font-medium uppercase", versionTypeClass(file.version.version_type))}>
              {file.version.version_type}
            </span>
          )}
        </span>
      ) : (
        <span className="text-xs text-muted-foreground">—</span>
      )}
      {update &&
        (mutable ? (
          <Button
            size="xs"
            variant="outline"
            className="border-sky-500/40 text-sky-700 dark:text-sky-300"
            disabled={updating}
            onClick={onUpdate}
            aria-label={`Update ${title} to ${update.latest.version_number ?? "the newest version"}`}
            title={`Update to ${update.latest.version_number ?? "the newest version"} (${update.latest.version_type ?? "release"})`}
          >
            {updating ? <Loader2 className="animate-spin" /> : <ArrowUpCircle />}
            <span className="max-w-32 truncate">{update.latest.version_number ?? "Update"}</span>
          </Button>
        ) : (
          <span className="inline-flex items-center gap-1 rounded-full bg-sky-500/10 px-1.5 text-[11px] font-medium text-sky-700 dark:text-sky-300">
            <ArrowUpCircle className="size-3" aria-hidden /> {update.latest.version_number}
          </span>
        ))}
    </div>
  );

  return (
    <TableRow data-state={selected ? "selected" : undefined} className={cn(!file.enabled && "text-muted-foreground")}>
      {env.canEdit && (
        <TableCell className="w-8 pr-0">
          <Checkbox
            checked={selected}
            onCheckedChange={(c) => onSelect(c === true)}
            disabled={file.source !== "modrinth"}
            aria-label={`Select ${title} for updating`}
            title={file.source !== "modrinth" ? "Only files identified on Modrinth can be updated" : undefined}
          />
        </TableCell>
      )}
      <TableCell className="max-w-0 min-w-36 py-2 sm:min-w-56">
        <div className="flex min-w-0 items-center gap-2.5">
          <ProjectIcon url={file.project?.icon_url} className={cn("size-8", !file.enabled && "opacity-50 grayscale")} />
          <div className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-1.5">
              <span className={cn("truncate font-medium", file.enabled ? "text-foreground" : "line-through decoration-muted-foreground/50")}>
                {title}
              </span>
              <SourceBadges file={file} env={env} />
            </div>
            <p className="truncate font-mono text-[11px] text-muted-foreground" title={file.filename}>
              {file.filename}
            </p>
            <div className="mt-1 sm:hidden">{versionEl}</div>
          </div>
        </div>
      </TableCell>
      <TableCell className="hidden py-2 sm:table-cell">{versionEl}</TableCell>
      <TableCell className="hidden py-2 text-xs tabular-nums text-muted-foreground md:table-cell">{formatBytes(file.size)}</TableCell>
      <TableCell className="hidden py-2 text-xs text-muted-foreground lg:table-cell" title={formatDateTime(file.modified)}>
        {relativeTime(file.modified)}
      </TableCell>
      <TableCell className="py-2">
        <Switch
          checked={enabled}
          onCheckedChange={(c) => toggle.mutate(c)}
          disabled={!mutable || toggle.isPending}
          aria-label={`${title} enabled`}
        />
      </TableCell>
      <TableCell className="py-2 text-right">
        <div className="flex items-center justify-end gap-0.5">
          {link && (
            <Button asChild variant="ghost" size="icon-sm">
              <a href={link} target="_blank" rel="noopener noreferrer" aria-label={`Open ${title} on ${file.source === "spigot" ? "SpigotMC" : "Modrinth"}`}>
                <ExternalLink />
              </a>
            </Button>
          )}
          {env.canEdit && (
            <ConfirmAction
              trigger={
                <Button variant="ghost" size="icon-sm" aria-label={`Remove ${title}`} disabled={!mutable || remove.isPending}>
                  {remove.isPending ? <Loader2 className="animate-spin" /> : <Trash2 />}
                </Button>
              }
              title={`Remove ${title}?`}
              description={
                <div className="grid gap-2">
                  <p>
                    <span className="font-mono text-foreground">{file.filename}</span> is deleted from{" "}
                    <span className="font-mono">/{env.directory}</span> permanently.
                  </p>
                  <p>
                    {file.source === "modrinth"
                      ? `It can be reinstalled from Modrinth later.`
                      : file.source === "spigot" || file.source === "hangar"
                        ? `It can be installed again from the ${file.source === "spigot" ? "SpigotMC" : "Hangar"} tab, possibly in a newer version.`
                        : `This file is not on Modrinth, so it can only be recovered from a backup.`}{" "}
                    To keep the file but stop loading it, disable it instead. Worlds may lose blocks or items this{" "}
                    {env.noun.one} added.
                  </p>
                </div>
              }
              confirmLabel="Remove"
              destructive
              confirmText={file.source === "modrinth" ? undefined : "delete"}
              onConfirm={async () => {
                await remove.mutateAsync();
              }}
            />
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}

export function InstalledContent({
  env,
  inventory,
  onBrowse,
  onUpload,
}: {
  env: ContentEnv;
  inventory: ContentInventory;
  onBrowse: () => void;
  onUpload: (() => void) | null;
}) {
  const [text, setText] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [sort, setSort] = useState<SortKey>("name");
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [checkRequested, setCheckRequested] = useState(false);
  const [rowUpdating, setRowUpdating] = useState<string | null>(null);
  const updates = useContentUpdates(env.instance.id, checkRequested);
  const canCheck = !!env.minecraftVersion;

  const updateMap = useMemo(() => new Map((updates.data?.updates ?? []).map((u) => [u.filename, u])), [updates.data]);
  const files = inventory.files;
  const known = useMemo(() => new Set(files.map((f) => f.filename)), [files]);
  const selection = useMemo(() => [...selected].filter((n) => known.has(n)), [selected, known]);

  const bulk = useUpdateFiles(env, () => setSelected(new Set()));
  const single = useUpdateFiles(env);
  const mutable = env.canEdit && !env.locked;

  const visible = useMemo(() => {
    const needle = text.trim().toLowerCase();
    const out = files.filter((f) => {
      if (status === "enabled" && !f.enabled) return false;
      if (status === "disabled" && f.enabled) return false;
      if (status === "manual" && f.source === "modrinth") return false;
      if (status === "updates" && !updateMap.has(f.filename)) return false;
      if (!needle) return true;
      return f.filename.toLowerCase().includes(needle) || (f.project?.title ?? "").toLowerCase().includes(needle);
    });
    const byName = (a: ContentFile, b: ContentFile) => displayTitle(a).localeCompare(displayTitle(b), undefined, { sensitivity: "base" });
    out.sort((a, b) => {
      if (sort === "size") return b.size - a.size || byName(a, b);
      if (sort === "modified") return (b.modified ?? 0) - (a.modified ?? 0) || byName(a, b);
      return byName(a, b);
    });
    return out;
  }, [files, text, status, sort, updateMap]);

  const selectable = visible.filter((f) => f.source === "modrinth");
  const allVisibleSelected = selectable.length > 0 && selectable.every((f) => selected.has(f.filename));
  const updatable = [...updateMap.keys()].filter((n) => known.has(n));

  const setOne = (name: string, on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(name);
      else next.delete(name);
      return next;
    });
  const setAllVisible = (on: boolean) =>
    setSelected((prev) => {
      const next = new Set(prev);
      for (const f of selectable) {
        if (on) next.add(f.filename);
        else next.delete(f.filename);
      }
      return next;
    });

  const runCheck = () => {
    if (!checkRequested) setCheckRequested(true);
    else void updates.refetch();
  };

  if (files.length === 0) {
    return (
      <EmptyState
        icon={<PackageSearch className="size-5" />}
        title={`No ${env.noun.many} installed`}
        description={
          <>
            Nothing in <span className="font-mono">/{env.directory}</span> yet. Find compatible {env.noun.many} for {env.platform}
            {env.minecraftVersion ? ` ${env.minecraftVersion}` : ""} on Modrinth, or upload a .jar you already have.
          </>
        }
        action={
          <div className="flex flex-wrap justify-center gap-2">
            <Button size="sm" onClick={onBrowse}>
              <Search /> Browse {env.noun.many}
            </Button>
            {onUpload && (
              <Button size="sm" variant="outline" onClick={onUpload}>
                <Upload /> Upload .jar
              </Button>
            )}
          </div>
        }
      />
    );
  }

  return (
    <div className="grid grid-cols-1 gap-3">
      {/* update check summary */}
      <div className="flex flex-col gap-2 rounded-lg border bg-muted/30 px-3 py-2 text-sm sm:flex-row sm:items-center">
        <div className="min-w-0 flex-1">
          {!checkRequested ? (
            <span className="text-muted-foreground">
              {canCheck
                ? `Check Modrinth for newer ${env.platform} ${env.minecraftVersion} versions of the ${inventory.summary.identified} identified ${env.noun.many}.`
                : `Update checks need a specific Minecraft version (this server follows the newest one).`}
            </span>
          ) : updates.isFetching ? (
            <span className="inline-flex items-center gap-2 text-muted-foreground">
              <Loader2 className="size-4 animate-spin" aria-hidden /> Checking {inventory.summary.identified} files on Modrinth…
            </span>
          ) : updates.error ? (
            <span className="text-destructive">Update check failed: {errorMessage(updates.error)}</span>
          ) : updates.data ? (
            <span>
              <span className={cn("font-medium", updatable.length ? "text-sky-700 dark:text-sky-300" : "text-emerald-700 dark:text-emerald-300")}>
                {updatable.length ? `${updatable.length} update${updatable.length === 1 ? "" : "s"} available` : "Everything is up to date"}
              </span>
              <span className="text-muted-foreground">
                {" "}
                · {updates.data.checked} checked
                {updates.data.unidentified ? ` · ${updates.data.unidentified} not on Modrinth` : ""} · {relativeTime(updates.dataUpdatedAt)}
              </span>
            </span>
          ) : null}
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {mutable && updatable.length > 0 && !updates.isFetching && (
            <BulkUpdateConfirm env={env} count={updatable.length} pending={bulk.isPending} label={`Update all (${updatable.length})`} onConfirm={() => bulk.mutate(updatable)} />
          )}
          <Button size="sm" variant="outline" onClick={runCheck} disabled={!canCheck || updates.isFetching}>
            <RefreshCw className={cn(updates.isFetching && "animate-spin")} /> {checkRequested ? "Check again" : "Check for updates"}
          </Button>
        </div>
      </div>

      {/* toolbar */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <div className="grid flex-1 gap-1">
          <Label htmlFor="content-filter" className="sr-only">
            Filter installed {env.noun.many}
          </Label>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <Input
              id="content-filter"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={`Filter ${inventory.summary.total} ${env.noun.many}…`}
              className="pl-8"
              type="search"
            />
          </div>
        </div>
        <div className="flex gap-2">
          <div className="grid gap-1">
            <Label htmlFor="content-status" className="sr-only">
              Show
            </Label>
            <Select value={status} onValueChange={(v) => setStatus(v as StatusFilter)}>
              <SelectTrigger id="content-status" className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All ({inventory.summary.total})</SelectItem>
                <SelectItem value="enabled">Enabled ({inventory.summary.enabled})</SelectItem>
                <SelectItem value="disabled">Disabled ({inventory.summary.disabled})</SelectItem>
                <SelectItem value="manual">Not on Modrinth ({inventory.summary.total - inventory.summary.identified})</SelectItem>
                {updates.data && <SelectItem value="updates">Updates ({updatable.length})</SelectItem>}
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-1">
            <Label htmlFor="content-sort" className="sr-only">
              Sort by
            </Label>
            <Select value={sort} onValueChange={(v) => setSort(v as SortKey)}>
              <SelectTrigger id="content-sort" className="w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="name">Sort: name</SelectItem>
                <SelectItem value="size">Sort: size</SelectItem>
                <SelectItem value="modified">Sort: changed</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
      </div>

      {/* bulk bar */}
      {env.canEdit && selection.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-primary/30 bg-primary/5 px-3 py-2 text-sm" role="region" aria-label="Bulk actions">
          <span className="font-medium">{selection.length} selected</span>
          <span className="text-xs text-muted-foreground">Updates move each file to its newest compatible version.</span>
          <div className="ml-auto flex gap-2">
            <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>
              Clear
            </Button>
            <BulkUpdateConfirm env={env} count={selection.length} pending={bulk.isPending} label="Update selected" onConfirm={() => bulk.mutate(selection)} />
          </div>
        </div>
      )}
      {bulk.isPending && (
        <Notice tone="info">Downloading and verifying new versions from Modrinth. This can take a while for many files.</Notice>
      )}

      {visible.length === 0 ? (
        <EmptyState title="Nothing matches" description="Try a different filter." className="py-8" />
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                {env.canEdit && (
                  <TableHead className="w-8 pr-0">
                    <Checkbox
                      checked={allVisibleSelected}
                      onCheckedChange={(c) => setAllVisible(c === true)}
                      disabled={selectable.length === 0}
                      aria-label="Select all identified files shown"
                    />
                  </TableHead>
                )}
                <TableHead>{env.noun.One}</TableHead>
                <TableHead className="hidden sm:table-cell">Version</TableHead>
                <TableHead className="hidden md:table-cell">Size</TableHead>
                <TableHead className="hidden lg:table-cell">Changed</TableHead>
                <TableHead>Enabled</TableHead>
                <TableHead className="text-right">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {visible.map((f) => (
                <InstalledRow
                  key={f.filename}
                  file={f}
                  env={env}
                  update={updateMap.get(f.filename)}
                  selected={selected.has(f.filename)}
                  onSelect={(on) => setOne(f.filename, on)}
                  updating={(single.isPending && rowUpdating === f.filename) || (bulk.isPending && (bulk.variables ?? []).includes(f.filename))}
                  onUpdate={() => {
                    setRowUpdating(f.filename);
                    single.mutate([f.filename]);
                  }}
                />
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        {inventory.summary.enabled} enabled · {inventory.summary.disabled} disabled · {inventory.summary.identified} identified on Modrinth ·{" "}
        {inventory.summary.manual} manual. Disabled files are renamed to <span className="font-mono">.jar.disabled</span> and not loaded.
      </p>
    </div>
  );
}
