/** Mods / plugins manager: installed files, Modrinth browser and jar upload. */
import { Blocks, Download, Package, Plus, RotateCw, Search, Upload } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuth } from "@/hooks/use-auth";
import { ApiError } from "@/lib/api";
import type { Instance, PermissionCode } from "@/lib/types";
import { useInstanceContext } from "./InstanceLayout";
import { BrowseContent } from "./content/BrowseContent";
import { isRunningLike, nouns, useContentInventory, useRestartHint, useRestartServer } from "./content/content-api";
import type { ContentReport } from "./content/content-report";
import { Notice, ReportCard, type ContentEnv } from "./content/content-ui";
import { InstalledContent } from "./content/InstalledContent";
import { UploadJar } from "./content/UploadJar";

type TabId = "installed" | "browse" | "upload";

function NoContentSupport({ instance }: { instance: Instance }) {
  const { can } = useAuth();
  const custom = instance.server_type === "CUSTOM";
  return (
    <EmptyState
      icon={<Blocks className="size-5" />}
      title={custom ? "Add-ons are not managed for custom servers" : "Vanilla servers cannot load mods or plugins"}
      description={
        custom ? (
          <p>
            This server runs custom software, so the panel cannot tell which mods or plugins it supports. Manage its files directly in
            the file manager.
          </p>
        ) : (
          <div className="grid gap-2 text-left sm:text-center">
            <p>Vanilla is Mojang's unmodified server software. To add content, the server needs a different type:</p>
            <ul className="grid gap-1 text-left">
              <li>
                <span className="font-medium text-foreground">Paper or Purpur</span> run <em>plugins</em> (Bukkit/Spigot/Paper).
                Players join with an unmodified game.
              </li>
              <li>
                <span className="font-medium text-foreground">Fabric, NeoForge or Forge</span> run <em>mods</em>. Players usually need
                the same mods installed.
              </li>
            </ul>
            <p>
              The type of an existing server cannot be changed. Create a new server and move the world over with a backup or the file
              manager.
            </p>
          </div>
        )
      }
      action={
        !custom && can("instance.create") ? (
          <Button asChild size="sm">
            <Link to="/servers/new">
              <Plus /> Create a Paper or Fabric server
            </Link>
          </Button>
        ) : custom && hasFiles(instance) ? (
          <Button asChild size="sm" variant="outline">
            <Link to={`/servers/${instance.id}/files`}>Open file manager</Link>
          </Button>
        ) : undefined
      }
    />
  );
}

function hasFiles(instance: Instance) {
  return instance.permissions.includes("instance.files.read");
}

function lockReason(instance: Instance): string | null {
  if (instance.archived_at) return "This server is archived; restore it before changing its content.";
  if (instance.active_job) return `Another operation is in progress (${instance.active_job.title}). Wait for it to finish.`;
  return null;
}

function ContentManager({ instance, can }: { instance: Instance; can: (code: PermissionCode) => boolean }) {
  const inventory = useContentInventory(instance.id);
  const [params, setParams] = useSearchParams();
  const canEdit = can("instance.content");
  const requested = params.get("tab") as TabId | null;
  const tab: TabId = requested === "browse" || (requested === "upload" && canEdit) ? requested : "installed";
  const [report, setReport] = useState<ContentReport | null>(null);
  const [browse, setBrowse] = useState({ key: 0, query: "" });
  const hint = useRestartHint(instance);
  const restart = useRestartServer(instance);
  const canControl = can("instance.control") && !instance.archived_at;
  const running = isRunningLike(instance);
  const kind = instance.content_kind === "plugins" ? "plugins" : "mods";
  const noun = nouns(kind);

  const setTab = (t: TabId) =>
    setParams(
      (p) => {
        const next = new URLSearchParams(p);
        if (t === "installed") next.delete("tab");
        else next.set("tab", t);
        return next;
      },
      { replace: true },
    );

  if (inventory.isLoading) return <LoadingState label={`Reading ${noun.many}…`} />;
  if (inventory.error || !inventory.data) {
    const notFound = inventory.error instanceof ApiError && inventory.error.status === 404;
    return (
      <ErrorState
        error={inventory.error ?? new Error("No data")}
        title={notFound ? `The ${noun.one} manager is not available on this panel version` : `Could not read the installed ${noun.many}`}
        onRetry={() => inventory.refetch()}
      />
    );
  }
  const inv = inventory.data;

  const env: ContentEnv = {
    instance,
    kind,
    noun,
    directory: inv.directory,
    platform: inv.platform,
    minecraftVersion: inv.minecraft_version,
    canEdit,
    locked: lockReason(instance),
    notify: (message, restartRequired, description) => {
      if (restartRequired) {
        hint.mark();
        toast.success(message, {
          description: description ?? `Restart the server to load the change — ${noun.many} are only loaded at startup.`,
          action: canControl ? { label: "Restart now", onClick: () => restart.mutate() } : undefined,
        });
      } else {
        toast.success(message, { description: description ?? (running ? undefined : "Takes effect the next time the server starts.") });
      }
    },
    showReport: setReport,
    browseFor: (query) => {
      setBrowse((b) => ({ key: b.key + 1, query }));
      setTab("browse");
    },
  };

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      {hint.pending && (
        <Notice
          tone="warning"
          title="Restart to load the changes"
          action={
            <>
              {canControl && (
                <Button size="sm" onClick={() => restart.mutate(undefined, { onSuccess: () => hint.clear() })} disabled={restart.isPending || !!instance.active_job}>
                  <RotateCw className={restart.isPending ? "animate-spin" : undefined} /> Restart now
                </Button>
              )}
              <Button size="sm" variant="ghost" onClick={hint.clear}>
                Dismiss
              </Button>
            </>
          }
        >
          {noun.Many} were changed while the server is running. Minecraft only loads {noun.many} when the server starts.
        </Notice>
      )}
      {canEdit && env.locked && <Notice tone="info">{env.locked}</Notice>}
      {inv.warnings.map((w) => (
        <Notice key={w} tone="warning">
          {w}
        </Notice>
      ))}
      {report && <ReportCard report={report} env={env} onDismiss={() => setReport(null)} />}

      <Tabs value={tab} onValueChange={(v) => setTab(v as TabId)}>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <TabsList aria-label={`${noun.Many} sections`}>
            <TabsTrigger value="installed" className="px-3">
              <Package /> Installed
              <span className="rounded-full bg-muted-foreground/15 px-1.5 text-[11px] tabular-nums">{inv.summary.total}</span>
            </TabsTrigger>
            <TabsTrigger value="browse" className="px-3">
              <Search /> Browse
            </TabsTrigger>
            {canEdit && (
              <TabsTrigger value="upload" className="px-3">
                <Upload /> Upload
              </TabsTrigger>
            )}
          </TabsList>
          <p className="text-xs text-muted-foreground">
            {inv.platform}
            {inv.minecraft_version ? ` · Minecraft ${inv.minecraft_version}` : ""} · <span className="font-mono">/{inv.directory}</span>
            {" · "}
            {running ? "changes need a restart" : "changes apply on next start"}
          </p>
        </div>

        <TabsContent value="installed" className="mt-2">
          <Section
            title={`Installed ${noun.many}`}
            description={`Jar files in /${inv.directory}, identified on Modrinth by their hash.`}
          >
            <InstalledContent
              env={env}
              inventory={inv}
              onBrowse={() => setTab("browse")}
              onUpload={canEdit ? () => setTab("upload") : null}
            />
          </Section>
        </TabsContent>
        <TabsContent value="browse" className="mt-2">
          <Section
            title={`Find ${noun.many}`}
            description={`Compatible ${noun.many} from Modrinth, downloaded straight to the server and verified against the published hash.`}
          >
            <BrowseContent key={browse.key} env={env} initialQuery={browse.query} />
          </Section>
        </TabsContent>
        {canEdit && (
          // kept mounted so uploads in progress survive switching tabs
          <TabsContent value="upload" forceMount className="mt-2 data-[state=inactive]:hidden">
            <Section
              title={`Upload ${noun.one} jars`}
              description={`For ${noun.many} that are not on Modrinth. Uploaded files are checked to target ${inv.platform}.`}
            >
              <UploadJar env={env} />
            </Section>
          </TabsContent>
        )}
      </Tabs>
      {!canEdit && (
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Download className="size-3.5" aria-hidden /> You can view the {noun.many} on this server; installing, updating and removing
          them requires the “Manage mods/plugins” permission.
        </p>
      )}
    </div>
  );
}

export default function Content() {
  const { instance, can } = useInstanceContext();
  if (instance.content_kind === "none") return <NoContentSupport instance={instance} />;
  return <ContentManager instance={instance} can={can} />;
}
