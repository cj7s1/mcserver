/**
 * Backup safeguards: the notice on a server's Overview and Backups pages and the small indicator in
 * the server lists. Both show only what lib/backup-health.ts decides (nothing for archived servers,
 * for viewers without the backup permission, or when a recent backup exists).
 */
import { AlertTriangle } from "lucide-react";
import { Link } from "react-router";
import { Badge } from "@/components/ui/badge";
import { backupWarning, backupWarningDetail, type BackupHealthFields } from "@/lib/backup-health";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";

type NoticeInstance = BackupHealthFields & Pick<Instance, "id">;

const LINK = "font-medium text-foreground underline underline-offset-2 hover:text-primary";

export function BackupNotice({
  instance,
  backupsLink = true,
  now,
  className,
}: {
  instance: NoticeInstance;
  /** false on the Backups page itself, where "Create backup" is right below */
  backupsLink?: boolean;
  now?: number;
  className?: string;
}) {
  const warning = backupWarning(instance, now);
  if (!warning) return null;
  // adding a backup schedule needs this next to instance.backup, which a viewer of this notice has
  const canSchedule = instance.permissions.includes("instance.schedules");
  return (
    <div
      role="status"
      className={cn("flex gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm", className)}
    >
      <AlertTriangle className="mt-0.5 size-5 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
      <div className="min-w-0">
        <p className="font-medium">{warning.title}</p>
        <p className="mt-1 text-muted-foreground">
          {backupWarningDetail(warning)}
          {!backupsLink && " Use “Create backup” below to take one now."}
        </p>
        <p className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
          {backupsLink && (
            <Link to={`/servers/${instance.id}/backups`} className={LINK}>
              Open Backups
            </Link>
          )}
          {!warning.scheduled ? (
            <span className="text-muted-foreground">
              <Link to={`/servers/${instance.id}/schedules`} className={LINK}>
                Schedules
              </Link>
              {canSchedule
                ? ": add a backup schedule there, for example the “Nightly backup” template."
                : ": someone who may manage schedules can add a nightly backup there."}
            </span>
          ) : (
            warning.kind === "stale" && (
              <Link to={`/servers/${instance.id}/schedules`} className={LINK}>
                Check Schedules
              </Link>
            )
          )}
        </p>
      </div>
    </div>
  );
}

/** Small amber badge for server rows and cards; links to the server's Backups page. */
export function BackupWarningBadge({ instance, now, className }: { instance: NoticeInstance; now?: number; className?: string }) {
  const warning = backupWarning(instance, now);
  if (!warning) return null;
  const hint = warning.scheduled ? "A backup schedule is enabled." : "No backup schedule is enabled.";
  return (
    <Badge asChild variant="outline" className={cn("border-amber-500/40 text-amber-700 dark:text-amber-300", className)}>
      <Link to={`/servers/${instance.id}/backups`} title={`${warning.title}. ${hint} Open Backups.`}>
        <AlertTriangle aria-hidden />
        {warning.label}
      </Link>
    </Badge>
  );
}
