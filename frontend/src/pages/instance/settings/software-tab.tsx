/** Minecraft / loader / Java versions. Version changes go through POST /instances/{id}/software (job with pre-update backup). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Coffee, Loader2, Package, Undo2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { JobProgress } from "@/components/JobProgress";
import { Section } from "@/components/PageHeader";
import { ErrorState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { api, ApiError, errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { qk, useInvalidateInstance, useJob } from "@/lib/queries";
import type { Instance, JavaInfo, Job, ServerTypeInfo, VersionEntry } from "@/lib/types";
import { Field, Notice, ReadOnlyNotice, usePatchInstance, useReportDirty, type TabProps } from "./settings-ui";
import { VersionPicker, type PickerOption } from "./version-picker";

const JAVA_VERSIONS = [8, 11, 16, 17, 21, 25];
const NO_CATALOG = new Set(["CUSTOM"]);

function versionKey(v: string): number[] | null {
  const m = /^(\d+(?:\.\d+)*)$/.exec(v.trim());
  return m ? m[1].split(".").map(Number) : null;
}

/** True when `to` is clearly older than `from` (both plain release numbers). */
function isDowngrade(from: string, to: string): boolean {
  const a = versionKey(from);
  const b = versionKey(to);
  if (!a || !b) return false;
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] ?? 0;
    const y = b[i] ?? 0;
    if (x !== y) return y < x;
  }
  return false;
}

function ModpackInfo({ instance }: { instance: Instance }) {
  const canChange = instance.permissions.includes("instance.content");
  const rows: [string, string][] = [
    ["Modpack", instance.modpack_name || instance.modpack_slug || "—"],
    ["Release", instance.modpack_version_name || instance.modpack_version_id || "—"],
    ["Provider", instance.modpack_provider || instance.source || "—"],
    ["Minecraft", instance.minecraft_version || "—"],
    ["Loader", [instance.modpack_loader || instance.loader, instance.loader_version].filter(Boolean).join(" ") || "—"],
  ];
  return (
    <Section
      title={
        <span className="inline-flex items-center gap-2">
          <Package className="size-4 text-muted-foreground" aria-hidden /> Modpack release
        </span>
      }
    >
      <div className="grid grid-cols-1 gap-4">
        <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-3 lg:grid-cols-5">
          {rows.map(([k, v]) => (
            <div key={k} className="min-w-0">
              <dt className="text-xs text-muted-foreground">{k}</dt>
              <dd className="truncate font-medium" title={v}>
                {v}
              </dd>
            </div>
          ))}
        </dl>
        <Notice
          title="Versions come from the pinned modpack release"
          action={
            <Button asChild size="sm" variant="outline">
              <Link to={`/servers/${instance.id}/modpack`}>
                Open Modpack tab <ArrowRight aria-hidden />
              </Link>
            </Button>
          }
        >
          The Minecraft and loader versions of a modpack server are defined by the modpack release it was installed from, so they
          cannot be changed here.{" "}
          {canChange
            ? "Update or switch the release on the Modpack tab: it shows what changes, backs up first and can roll back."
            : "The Modpack tab lists the available releases; changing the release needs the “Manage mods/plugins” permission."}{" "}
          The Java version below can still be changed.
        </Notice>
      </div>
    </Section>
  );
}

export function SoftwareTab({ instance, can, onDirtyChange }: TabProps) {
  const canEdit = can("instance.configure");
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const patch = usePatchInstance(instance.id);
  const st = instance.server_type;
  const versionsEditable = !instance.is_modpack && !NO_CATALOG.has(st);

  const [mcDraft, setMcDraft] = useState<string | null>(null);
  const [loaderDraft, setLoaderDraft] = useState<string | null>(null);
  const [javaDraft, setJavaDraft] = useState<number | null>(null);
  const [snapshots, setSnapshots] = useState(false);
  const [backup, setBackup] = useState(true);
  const [startAfter, setStartAfter] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  /** Message of a 409 downgrade_unconfirmed response; opens the explicit "Downgrade anyway" confirmation. */
  const [downgradeMessage, setDowngradeMessage] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);

  const mc = mcDraft ?? instance.minecraft_version;
  const mcChanged = mc !== instance.minecraft_version;
  const loader = loaderDraft ?? (mcChanged ? "" : instance.loader_version);

  const types = useQuery({
    queryKey: ["catalog", "server-types"],
    queryFn: () => api.get<{ server_types: ServerTypeInfo[] }>("/catalog/server-types"),
    staleTime: 3_600_000,
    enabled: versionsEditable,
  });
  const typeInfo = types.data?.server_types.find((t) => t.id === st);
  const hasLoader = versionsEditable && st !== "VANILLA" && (typeInfo ? !!typeInfo.loader_label : true);

  const versions = useQuery({
    queryKey: ["catalog", "versions", st, snapshots],
    queryFn: () =>
      api.get<{ server_type: string; latest_release: string; versions: VersionEntry[] }>("/catalog/versions", {
        server_type: st,
        snapshots: snapshots ? "true" : undefined,
      }),
    staleTime: 600_000,
    enabled: versionsEditable,
  });
  const loaders = useQuery({
    queryKey: ["catalog", "loaders", st, mc],
    queryFn: () => api.get<{ versions: VersionEntry[] }>("/catalog/loaders", { server_type: st, minecraft_version: mc }),
    staleTime: 600_000,
    enabled: hasLoader && !!mc && mc.toUpperCase() !== "LATEST",
  });
  const java = useQuery({
    queryKey: ["catalog", "java", mc, st],
    queryFn: () => api.get<JavaInfo>("/catalog/java", { minecraft_version: mc, server_type: st }),
    staleTime: 600_000,
    enabled: !!mc,
  });

  const javaValue = javaDraft ?? (mcChanged && java.data ? java.data.java_version : instance.java_version);
  const loaderChanged = versionsEditable && loader !== instance.loader_version;
  const javaChanged = javaValue !== instance.java_version;
  const versionChange = versionsEditable && (mcChanged || loaderChanged);
  const dirty = versionChange || javaChanged;
  const downgrade = mcChanged && isDowngrade(instance.minecraft_version, mc);
  useReportDirty(dirty, onDirtyChange);

  const job = useJob(jobId);
  const jobStatus = job.data?.status;
  useEffect(() => {
    if (jobStatus !== "succeeded" && jobStatus !== "failed") return;
    if (jobStatus === "succeeded") toast.success("Software updated");
    qc.invalidateQueries({ queryKey: qk.instance(instance.id) });
    qc.invalidateQueries({ queryKey: qk.instances });
    qc.invalidateQueries({ queryKey: qk.jobs });
  }, [jobStatus, instance.id, qc]);

  const update = useMutation({
    mutationFn: (allowDowngrade: boolean) =>
      api.post<Job>(`/instances/${instance.id}/software`, {
        minecraft_version: mc,
        loader_version: loader,
        java_version: javaValue,
        backup,
        start: startAfter,
        allow_downgrade: allowDowngrade,
      }),
  });

  const versionOptions = useMemo<PickerOption[]>(() => {
    const list: PickerOption[] = (versions.data?.versions ?? []).map((v) => ({
      value: v.version,
      label: v.version,
      badge: v.version === instance.minecraft_version ? "current" : v.version === versions.data?.latest_release ? "latest" : !v.stable ? (v.type ?? "unstable") : undefined,
      detail: v.released ? new Date(v.released).toLocaleDateString() : undefined,
    }));
    if (instance.minecraft_version && !list.some((o) => o.value === instance.minecraft_version)) {
      list.unshift({ value: instance.minecraft_version, label: instance.minecraft_version, badge: "current" });
    }
    if (!list.some((o) => o.value.toUpperCase() === "LATEST")) {
      list.unshift({ value: "LATEST", label: "LATEST", detail: "always the newest release on recreate" });
    }
    return list;
  }, [versions.data, instance.minecraft_version]);

  const loaderOptions = useMemo<PickerOption[]>(() => {
    const list: PickerOption[] = [{ value: "", label: "Latest (automatic)", detail: "picked at install" }];
    for (const v of loaders.data?.versions ?? []) {
      list.push({
        value: v.version,
        label: v.version,
        badge:
          !mcChanged && v.version === instance.loader_version
            ? "current"
            : v.recommended
              ? "recommended"
              : v.latest
                ? "latest"
                : !v.stable
                  ? (v.channel ?? "beta")
                  : undefined,
      });
    }
    if (!mcChanged && instance.loader_version && !list.some((o) => o.value === instance.loader_version)) {
      list.splice(1, 0, { value: instance.loader_version, label: instance.loader_version, badge: "current" });
    }
    return list;
  }, [loaders.data, instance.loader_version, mcChanged]);

  const busy = !!instance.active_job || update.isPending || patch.isPending || (!!jobId && !jobStatus) || jobStatus === "queued" || jobStatus === "running";
  const disabled = !canEdit || busy || !!instance.archived_at;
  const javaTooLow = java.data ? javaValue < java.data.required : false;
  const forgeJava8 = java.data?.java_version === 8 && /java 8/i.test(java.data.reason) && javaValue !== 8;
  const loaderLabel = typeInfo?.loader_label ?? "Loader version";

  const discard = () => {
    setMcDraft(null);
    setLoaderDraft(null);
    setJavaDraft(null);
    setDowngradeMessage(null);
    update.reset();
    patch.reset();
  };

  /** POST /software; the first attempt never allows a downgrade, so the backend decides and we ask explicitly. */
  const submit = async (allowDowngrade: boolean) => {
    try {
      const j = await update.mutateAsync(allowDowngrade);
      setJobId(j.id);
      setMcDraft(null);
      setLoaderDraft(null);
      setJavaDraft(null);
      setDowngradeMessage(null);
      invalidate(instance.id);
      toast.success(allowDowngrade ? "Downgrade started" : "Update started");
    } catch (err) {
      if (!allowDowngrade && err instanceof ApiError && err.code === "downgrade_unconfirmed") {
        setDowngradeMessage(err.message);
        return;
      }
      toast.error(errorMessage(err));
      if (allowDowngrade) throw err;
    }
  };

  const apply = () => {
    if (versionChange) {
      setConfirmOpen(true);
      return;
    }
    if (javaChanged) {
      patch.mutate(
        { java_version: javaValue },
        {
          onSuccess: () => {
            setJavaDraft(null);
            toast.success(`Java ${javaValue} selected. Restart the server to apply it.`);
          },
          onError: (e) => toast.error(errorMessage(e)),
        },
      );
    }
  };

  return (
    <div className="grid grid-cols-1 gap-4">
      {!canEdit && <ReadOnlyNotice what="change versions" />}
      {instance.is_modpack && <ModpackInfo instance={instance} />}
      {st === "CUSTOM" && (
        <Notice title="Custom server">This server runs a custom server jar, so its Minecraft version is not managed by the panel.</Notice>
      )}

      {jobId && (
        <div className="grid grid-cols-1 gap-2">
          <JobProgress jobId={jobId} showLog={jobStatus === "failed"} />
          {(jobStatus === "succeeded" || jobStatus === "failed") && (
            <Button variant="ghost" size="xs" className="justify-self-end" onClick={() => setJobId(null)}>
              Dismiss
            </Button>
          )}
        </div>
      )}

      {versionsEditable && (
        <Section
          title="Minecraft version"
          description={`${typeInfo?.label ?? st} server. Changing versions recreates the container with the new server software.`}
          actions={
            <div className="flex items-center gap-2">
              <Switch id="show-snapshots" size="sm" checked={snapshots} onCheckedChange={setSnapshots} />
              <Label htmlFor="show-snapshots" className="text-xs font-normal">
                Show snapshots
              </Label>
            </div>
          }
        >
          {versions.error ? (
            <ErrorState error={versions.error} title="Could not load versions" onRetry={() => versions.refetch()} />
          ) : (
            <div className="grid grid-cols-1 gap-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <Field id="mc-version" label="Minecraft version" hint={`Installed: ${instance.minecraft_version}`}>
                  <VersionPicker
                    id="mc-version"
                    value={mc}
                    options={versionOptions}
                    loading={versions.isLoading}
                    disabled={disabled}
                    onChange={(v) => {
                      setMcDraft(v === instance.minecraft_version ? null : v);
                      setLoaderDraft(null);
                      setJavaDraft(null);
                      setDowngradeMessage(null);
                    }}
                  />
                </Field>
                {hasLoader && (
                  <Field
                    id="loader-version"
                    label={loaderLabel}
                    hint={
                      loaders.error
                        ? `Could not load versions: ${errorMessage(loaders.error)}`
                        : instance.loader_version && !mcChanged
                          ? `Installed: ${instance.loader_version}`
                          : "Automatic picks the newest compatible build when the container is created."
                    }
                  >
                    <VersionPicker
                      id="loader-version"
                      value={loader}
                      options={loaderOptions}
                      loading={loaders.isLoading}
                      disabled={disabled || mc.toUpperCase() === "LATEST"}
                      placeholder="Latest (automatic)"
                      onChange={(v) => setLoaderDraft(v)}
                    />
                  </Field>
                )}
              </div>
              {versionChange && (
                <Notice tone={downgrade ? "danger" : "warning"} title={downgrade ? "This is a downgrade" : "Back up before changing versions"}>
                  {downgrade
                    ? `Going from ${instance.minecraft_version} to ${mc} can corrupt worlds: older versions cannot read chunks saved by newer ones. Prefer restoring a backup made before the upgrade.`
                    : "Upgrades convert world data and cannot be undone without a backup. Mods and plugins must support the new version too."}
                </Notice>
              )}
            </div>
          )}
        </Section>
      )}

      <Section
        title={
          <span className="inline-flex items-center gap-2">
            <Coffee className="size-4 text-muted-foreground" aria-hidden /> Java
          </span>
        }
        description="The Java runtime image the server runs on."
      >
        <div className="grid grid-cols-1 gap-4">
          <Field
            id="java-version"
            label="Java version"
            hint={
              java.data
                ? `Recommended for ${mc}: Java ${java.data.java_version} (requires ${java.data.required}+ · ${java.data.reason}).`
                : java.isLoading
                  ? "Checking the recommended Java version…"
                  : java.error
                    ? `Could not determine the recommended version: ${errorMessage(java.error)}`
                    : undefined
            }
          >
            <Select value={String(javaValue)} onValueChange={(v) => setJavaDraft(Number(v))} disabled={disabled}>
              <SelectTrigger id="java-version" className="w-full sm:w-80">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {JAVA_VERSIONS.map((v) => (
                  <SelectItem key={v} value={String(v)}>
                    Java {v}
                    {java.data?.java_version === v ? " (recommended)" : ""}
                    {v === instance.java_version ? " · current" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          {javaTooLow && java.data && (
            <Notice tone="danger" title={`Java ${javaValue} is too old for Minecraft ${mc}`}>
              This version needs Java {java.data.required} or newer. The server will fail to start.
            </Notice>
          )}
          {forgeJava8 && (
            <Notice tone="warning">{java.data?.reason}. Newer Java versions usually crash these servers at startup.</Notice>
          )}
          {javaChanged && !versionChange && (
            <p className="text-xs text-muted-foreground">Saved immediately; takes effect when the server is restarted.</p>
          )}
        </div>
      </Section>

      {canEdit && dirty && (
        <div className="sticky bottom-3 z-20 flex flex-wrap items-center gap-2 rounded-xl border bg-card/95 px-4 py-2.5 shadow-lg backdrop-blur">
          <span className="text-sm font-medium">
            {versionChange
              ? `Change to ${typeInfo?.label ?? st} ${mc}${loader ? ` · ${loader}` : ""} · Java ${javaValue}`
              : `Switch to Java ${javaValue}`}
          </span>
          <div className="ml-auto flex gap-2">
            <Button variant="outline" size="sm" onClick={discard} disabled={busy}>
              <Undo2 /> Discard
            </Button>
            <Button size="sm" onClick={apply} disabled={busy || !!instance.archived_at}>
              {patch.isPending || update.isPending ? <Loader2 className="animate-spin" /> : null}
              {versionChange ? "Update server…" : "Save Java version"}
            </Button>
          </div>
          {instance.active_job && <p className="w-full text-xs text-muted-foreground">Wait for the running job to finish first.</p>}
        </div>
      )}

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={downgrade ? `Downgrade to Minecraft ${mc}?` : `Update to Minecraft ${mc}?`}
        confirmLabel={downgrade ? "Continue" : "Update server"}
        destructive={downgrade}
        description={
          <div className="grid gap-2 text-sm">
            <p>
              {instance.minecraft_version}
              {instance.loader_version ? ` · ${instance.loader_version}` : ""} · Java {instance.java_version} → {mc}
              {loader ? ` · ${loader}` : ""} · Java {javaValue}
            </p>
            <ol className="list-decimal space-y-0.5 pl-5 text-muted-foreground">
              <li>The server is stopped if it is running.</li>
              {backup && <li>A pre-update backup is created (shown on the Backups tab).</li>}
              <li>The container is recreated with the new version; the server software is downloaded on the next start.</li>
            </ol>
            {downgrade && <p className="font-medium text-destructive">Downgrades can corrupt the world permanently.</p>}
            {!backup && <p className="font-medium text-destructive">No backup will be made — you cannot roll back.</p>}
          </div>
        }
        onConfirm={() => submit(false)}
      >
        <div className="grid gap-3 rounded-lg border bg-muted/30 p-3">
          <div className="flex items-start gap-2">
            <Checkbox id="sw-backup" checked={backup} onCheckedChange={(v) => setBackup(v === true)} />
            <Label htmlFor="sw-backup" className="grid gap-0.5 font-normal">
              <span className="font-medium">Back up first (recommended)</span>
              <span className="text-xs text-muted-foreground">Takes longer for large worlds.</span>
            </Label>
          </div>
          <div className="flex items-start gap-2">
            <Checkbox id="sw-start" checked={startAfter} onCheckedChange={(v) => setStartAfter(v === true)} />
            <Label htmlFor="sw-start" className="grid gap-0.5 font-normal">
              <span className="font-medium">Start the server afterwards</span>
              <span className="text-xs text-muted-foreground">A server that was running is started again either way.</span>
            </Label>
          </div>
        </div>
      </ConfirmDialog>

      <ConfirmDialog
        open={downgradeMessage !== null}
        onOpenChange={(o) => !o && setDowngradeMessage(null)}
        title="Downgrade anyway?"
        confirmLabel="Downgrade anyway"
        destructive
        description={
          <div className="grid gap-2 text-sm">
            <p className="font-medium text-destructive">{downgradeMessage}</p>
            <p>
              {instance.minecraft_version} → {mc}. Chunks and player data saved by the newer version may fail to load or be reset,
              and some blocks and items can disappear. Restoring a backup made before the upgrade is the safer way back.
            </p>
            <p className="text-muted-foreground">
              {backup
                ? "A pre-update backup is created first, so you can restore the current state if the downgrade goes wrong."
                : "No backup will be made: there is no way back if the world is damaged."}
            </p>
          </div>
        }
        onConfirm={() => submit(true)}
      />

      {instance.installed_at && (
        <p className="text-xs text-muted-foreground">Server software first installed {formatDateTime(instance.installed_at)}.</p>
      )}
    </div>
  );
}
