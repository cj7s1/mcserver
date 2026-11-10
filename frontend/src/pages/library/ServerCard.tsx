import { AlertTriangle } from "lucide-react";
import { Link } from "react-router";
import { BackupWarningBadge } from "@/components/BackupNotice";
import { ServerIcon } from "@/components/ServerIcon";
import { StatusBadge } from "@/components/StatusBadge";
import { Progress } from "@/components/ui/progress";
import { formatDateTime, formatMemoryMb, relativeTime } from "@/lib/format";
import { gameLabel } from "@/lib/games";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";
import { softwareLabel } from "../instance/InstanceLayout";
import { ServerQuickActions } from "./ServerActions";
import { cardNote, loaderText, portText } from "./server-utils";

/** Grid card for one server. The whole card is clickable via a stretched link on the name. */
export function ServerCard({ instance }: { instance: Instance }) {
  const job = instance.active_job;
  const software = softwareLabel(instance);
  const created = instance.archived_at ?? instance.created_at;
  const note = job ? null : cardNote(instance);
  return (
    <article className="relative flex flex-col gap-3 rounded-xl border bg-card p-4 text-card-foreground transition-colors hover:border-primary/40 hover:bg-muted/20 has-[a[data-card-link]:focus-visible]:ring-3 has-[a[data-card-link]:focus-visible]:ring-ring/50">
      <div className="flex items-start gap-3">
        <ServerIcon url={instance.icon_url} name={instance.name} type={instance.server_type} className="size-11" />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-sm font-semibold leading-snug" title={instance.name}>
            <Link
              to={`/servers/${instance.id}`}
              data-card-link
              className="outline-none after:absolute after:inset-0 after:rounded-xl after:content-['']"
            >
              {instance.name}
            </Link>
          </h3>
          <p className="truncate text-xs text-muted-foreground" title={software}>
            {software}
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            <StatusBadge status={instance.status} title={instance.status_detail || undefined} />
            <BackupWarningBadge instance={instance} className="relative z-10" />
          </div>
        </div>
      </div>

      {instance.description && <p className="line-clamp-2 text-xs text-muted-foreground">{instance.description}</p>}

      <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
        <div className="min-w-0">
          <dt className="text-muted-foreground">{gameLabel(instance)}</dt>
          <dd className="truncate font-medium tabular-nums">{instance.minecraft_version || "—"}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-muted-foreground">Loader</dt>
          <dd className="truncate font-medium" title={loaderText(instance)}>
            {loaderText(instance)}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-muted-foreground">Memory</dt>
          <dd
            className="truncate font-medium tabular-nums"
            title={`${formatMemoryMb(instance.container_memory_mb)} container limit`}
          >
            {formatMemoryMb(instance.memory_mb)}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-muted-foreground">Port</dt>
          <dd
            className="truncate font-medium tabular-nums"
            title={instance.extra_ports?.length ? `${instance.extra_ports.length} extra port(s)` : undefined}
          >
            {portText(instance)}
          </dd>
        </div>
      </dl>

      {job ? (
        <div className="grid gap-1" aria-live="polite">
          <div className="flex justify-between gap-2 text-xs text-muted-foreground">
            <span className="truncate">{job.message || job.title}</span>
            <span className="tabular-nums">{Math.round((job.progress ?? 0) * 100)}%</span>
          </div>
          <Progress value={(job.progress ?? 0) * 100} className="h-1" aria-label={`${job.title} progress`} />
        </div>
      ) : note?.tone === "warning" ? (
        <p className="flex items-center gap-1.5 text-xs text-amber-700 dark:text-amber-300">
          <AlertTriangle className="size-3.5 shrink-0" aria-hidden /> {note.text}
        </p>
      ) : note ? (
        <p className={cn("line-clamp-2 text-xs", note.tone === "error" ? "text-destructive" : "text-muted-foreground")}>
          {note.text}
        </p>
      ) : null}

      <div className="mt-auto flex items-center justify-between gap-2 border-t pt-3">
        <span className="truncate text-xs text-muted-foreground" title={formatDateTime(created)}>
          {instance.archived_at ? "Archived" : "Created"} {relativeTime(created)}
        </span>
        <ServerQuickActions instance={instance} className="relative z-10" />
      </div>
    </article>
  );
}
