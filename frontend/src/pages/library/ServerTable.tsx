import { ChevronRight } from "lucide-react";
import type { MouseEvent } from "react";
import { Link, useNavigate } from "react-router";
import { BackupWarningBadge } from "@/components/BackupNotice";
import { ServerIcon } from "@/components/ServerIcon";
import { StatusBadge } from "@/components/StatusBadge";
import { Progress } from "@/components/ui/progress";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatDateTime, formatMemoryMb, relativeTime } from "@/lib/format";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";
import { softwareLabel } from "../instance/InstanceLayout";
import { ServerQuickActions } from "./ServerActions";
import { loaderText, portText } from "./server-utils";

export interface ServerGroup {
  key: string;
  label: string;
  items: Instance[];
}

const COLS = 8;

/** Compact list view. Groups render as collapsible <tbody> sections when `grouped` is set. */
export function ServerTable({
  groups,
  grouped,
  collapsed,
  onToggle,
}: {
  groups: ServerGroup[];
  grouped: boolean;
  collapsed: Set<string>;
  onToggle: (key: string) => void;
}) {
  return (
    <div className="min-w-0 rounded-xl border bg-card">
      <Table className="text-[13px]">
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead className="pl-4">Server</TableHead>
            <TableHead className="hidden sm:table-cell">Status</TableHead>
            <TableHead className="hidden sm:table-cell">Minecraft</TableHead>
            <TableHead className="hidden lg:table-cell">Loader</TableHead>
            <TableHead className="hidden text-right md:table-cell">Memory</TableHead>
            <TableHead className="hidden text-right md:table-cell">Port</TableHead>
            <TableHead className="hidden xl:table-cell">Created</TableHead>
            <TableHead className="pr-4 text-right">
              <span className="sr-only">Actions</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        {groups.map((g) => {
          const isCollapsed = grouped && collapsed.has(g.key);
          return (
            <TableBody key={g.key}>
              {grouped && (
                <TableRow className="bg-muted/40 hover:bg-muted/60">
                  <th colSpan={COLS} scope="rowgroup" className="p-0 text-left font-normal">
                    <GroupHeaderButton
                      label={g.label}
                      items={g.items}
                      collapsed={isCollapsed}
                      onToggle={() => onToggle(g.key)}
                      className="px-4 py-2"
                    />
                  </th>
                </TableRow>
              )}
              {!isCollapsed && g.items.map((i) => <ServerTableRow key={i.id} instance={i} />)}
            </TableBody>
          );
        })}
      </Table>
    </div>
  );
}

function ServerTableRow({ instance }: { instance: Instance }) {
  const navigate = useNavigate();
  const job = instance.active_job;
  const onRowClick = (e: MouseEvent<HTMLTableRowElement>) => {
    const target = e.target as HTMLElement;
    // ignore clicks from portaled menus and from interactive children
    if (!e.currentTarget.contains(target) || target.closest("a,button,input,[role=menuitem]")) return;
    navigate(`/servers/${instance.id}`);
  };
  return (
    <TableRow className="cursor-pointer" onClick={onRowClick}>
      <TableCell className="pl-4 max-sm:w-full max-sm:max-w-0">
        <div className="flex min-w-0 items-center gap-2.5">
          <ServerIcon
            url={instance.icon_url}
            name={instance.name}
            type={instance.server_type}
            className="size-8 rounded-md text-xs"
          />
          <div className="min-w-0 sm:max-w-[18rem]">
            <Link to={`/servers/${instance.id}`} className="block truncate font-medium hover:underline" title={instance.name}>
              {instance.name}
            </Link>
            <div className="truncate text-xs text-muted-foreground" title={softwareLabel(instance)}>
              {softwareLabel(instance)}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-1.5 sm:hidden">
              <StatusBadge status={instance.status} title={instance.status_detail || undefined} />
              <BackupWarningBadge instance={instance} />
            </div>
          </div>
        </div>
      </TableCell>
      <TableCell className="hidden sm:table-cell">
        <div className="grid justify-items-start gap-1">
          <StatusBadge status={instance.status} title={instance.status_detail || undefined} />
          <BackupWarningBadge instance={instance} />
          {job && (
            <div className="flex items-center gap-1.5" title={job.message || job.title}>
              <Progress value={(job.progress ?? 0) * 100} className="h-1 w-16" aria-label={`${job.title} progress`} />
              <span className="text-[11px] tabular-nums text-muted-foreground">{Math.round((job.progress ?? 0) * 100)}%</span>
            </div>
          )}
        </div>
      </TableCell>
      <TableCell className="hidden tabular-nums sm:table-cell">{instance.minecraft_version || "—"}</TableCell>
      <TableCell className="hidden max-w-[12rem] truncate lg:table-cell">{loaderText(instance)}</TableCell>
      <TableCell className="hidden text-right tabular-nums md:table-cell" title={`${formatMemoryMb(instance.container_memory_mb)} container limit`}>
        {formatMemoryMb(instance.memory_mb)}
      </TableCell>
      <TableCell className="hidden text-right tabular-nums md:table-cell">{portText(instance)}</TableCell>
      <TableCell className="hidden text-muted-foreground xl:table-cell" title={formatDateTime(instance.created_at)}>
        {relativeTime(instance.created_at)}
      </TableCell>
      <TableCell className="pr-4">
        <ServerQuickActions instance={instance} inlineLabel={false} className="justify-end" />
      </TableCell>
    </TableRow>
  );
}

export function GroupHeaderButton({
  label,
  items,
  collapsed,
  onToggle,
  controls,
  className,
}: {
  label: string;
  items: Instance[];
  collapsed: boolean;
  onToggle: () => void;
  controls?: string;
  className?: string;
}) {
  const running = items.filter((i) => i.status === "running" || i.status === "unhealthy").length;
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={!collapsed}
      aria-controls={controls}
      className={cn(
        "flex w-full items-center gap-2 rounded-md text-left text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
        className,
      )}
    >
      <ChevronRight
        className={cn("size-4 shrink-0 text-muted-foreground transition-transform", !collapsed && "rotate-90")}
        aria-hidden
      />
      <span className="truncate font-medium">{label}</span>
      <span className="rounded-full bg-muted px-1.5 text-xs tabular-nums text-muted-foreground">{items.length}</span>
      {running > 0 && <span className="text-xs text-emerald-700 dark:text-emerald-400">{running} running</span>}
    </button>
  );
}
