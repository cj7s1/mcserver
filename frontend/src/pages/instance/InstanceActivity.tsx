import { useInfiniteQuery } from "@tanstack/react-query";
import { CheckCircle2, Loader2, Lock, Search, ShieldAlert, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { AuditEvent, Instance, Job } from "@/lib/types";
import { cn } from "@/lib/utils";
import { InstanceJobItem } from "./activity/InstanceJobItem";
import { useInstanceContext } from "./InstanceLayout";
import { isJobActive, useInstanceJobs } from "./overview/instance-jobs";
import { capitalize, useNow } from "./overview/overview-utils";

type JobFilter = "all" | "active" | "failed" | "succeeded";

const JOB_FILTERS: { value: JobFilter; label: string; test: (j: Job) => boolean }[] = [
  { value: "all", label: "All", test: () => true },
  { value: "active", label: "Active", test: isJobActive },
  { value: "failed", label: "Failed", test: (j) => j.status === "failed" },
  { value: "succeeded", label: "Succeeded", test: (j) => j.status === "succeeded" },
];

function JobsSection({ instance, now, canControl }: { instance: Instance; now: number; canControl: boolean }) {
  const jobs = useInstanceJobs(instance.id, instance.active_job);
  const [filter, setFilter] = useState<JobFilter>("all");
  const [text, setText] = useState("");
  const counts = useMemo(() => {
    const out: Record<JobFilter, number> = { all: 0, active: 0, failed: 0, succeeded: 0 };
    for (const j of jobs.data ?? []) for (const f of JOB_FILTERS) if (f.test(j)) out[f.value]++;
    return out;
  }, [jobs.data]);
  const q = text.trim().toLowerCase();
  const test = JOB_FILTERS.find((f) => f.value === filter)!.test;
  const list = (jobs.data ?? []).filter(
    (j) => test(j) && (!q || `${j.title} ${j.kind} ${j.user_name ?? ""} ${j.message} ${j.error}`.toLowerCase().includes(q)),
  );

  return (
    <Section
      title="Jobs"
      description="Long-running operations for this server, newest first (latest 50). Expand a job to see its log."
      className="min-w-0"
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <ToggleGroup
          type="single"
          size="sm"
          variant="outline"
          spacing={0}
          value={filter}
          onValueChange={(v) => v && setFilter(v as JobFilter)}
          aria-label="Filter jobs by status"
        >
          {JOB_FILTERS.map((f) => (
            <ToggleGroupItem key={f.value} value={f.value}>
              {f.label}
              <span className="ml-1 text-muted-foreground tabular-nums">{counts[f.value]}</span>
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        <InputGroup className="w-full sm:ml-auto sm:w-64">
          <InputGroupAddon>
            <Search aria-hidden />
          </InputGroupAddon>
          <InputGroupInput value={text} onChange={(e) => setText(e.target.value)} placeholder="Filter jobs…" aria-label="Filter jobs by text" />
        </InputGroup>
      </div>
      {jobs.isLoading ? (
        <LoadingState label="Loading jobs…" className="py-8" />
      ) : jobs.error ? (
        <ErrorState error={jobs.error} onRetry={() => jobs.refetch()} title="Could not load jobs" />
      ) : !jobs.data?.length ? (
        <EmptyState title="No jobs yet" description="Installs, starts and stops, backups, restores and updates for this server are listed here." />
      ) : list.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">No jobs match the current filter.</p>
      ) : (
        <ul className="grid gap-2">
          {list.map((j) => (
            <InstanceJobItem
              key={j.id}
              job={j}
              now={now}
              defaultOpen={j.id === instance.active_job?.id}
              canAbandon={canControl}
            />
          ))}
        </ul>
      )}
    </Section>
  );
}

const AUDIT_PAGE = 100;

function actionParts(action: string): { scope: string; verb: string } {
  const [scope, ...rest] = action.split(".");
  return { scope, verb: capitalize(rest.join(" ").replace(/_/g, " ")) || action };
}

function isEmptyValue(v: unknown): boolean {
  return v === null || v === undefined || v === "" || (Array.isArray(v) && v.length === 0);
}

function DetailValue({ value }: { value: unknown }) {
  const text =
    typeof value === "string"
      ? value
      : Array.isArray(value) && value.every((x) => typeof x !== "object")
        ? value.join(", ")
        : JSON.stringify(value);
  return <span className="break-all">{text.length > 160 ? `${text.slice(0, 160)}…` : text}</span>;
}

function AuditDetails({ event }: { event: AuditEvent }) {
  const entries = Object.entries(event.details ?? {}).filter(([, v]) => !isEmptyValue(v));
  if (!entries.length) return <span className="text-muted-foreground">—</span>;
  return (
    <dl className="grid gap-0.5">
      {entries.map(([k, v]) =>
        k === "command" && typeof v === "string" ? (
          <div key={k} className="font-mono text-xs">
            <span className="text-muted-foreground">&gt; </span>
            <DetailValue value={v} />
          </div>
        ) : (
          <div key={k} className="flex min-w-0 gap-1.5 text-xs">
            <dt className="shrink-0 text-muted-foreground">{k.replace(/_/g, " ")}:</dt>
            <dd className="min-w-0 font-mono">
              <DetailValue value={v} />
            </dd>
          </div>
        ),
      )}
    </dl>
  );
}

function AuditSection({ instance, now }: { instance: Instance; now: number }) {
  const audit = useInfiniteQuery({
    queryKey: [...qk.audit, "instance", instance.id],
    queryFn: ({ pageParam }) =>
      api.get<AuditEvent[]>("/audit", { instance: instance.id, limit: AUDIT_PAGE, before: pageParam ?? undefined }),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => (last.length === AUDIT_PAGE ? last[last.length - 1].id : null),
    refetchInterval: 30_000,
    retry: false,
  });
  const forbidden = audit.error instanceof ApiError && audit.error.status === 403;
  const events = audit.data?.pages.flat() ?? [];

  return (
    <Section title="Audit log" description="Who changed or controlled this server, and when" className="min-w-0">
      {audit.isLoading ? (
        <LoadingState label="Loading audit events…" className="py-8" />
      ) : forbidden ? (
        <div className="flex items-start gap-3 rounded-lg bg-muted/50 p-4 text-sm text-muted-foreground">
          <Lock className="mt-0.5 size-4 shrink-0" aria-hidden />
          <p>
            The audit log for this server is only visible to users with the <strong className="font-medium text-foreground">Configure</strong>{" "}
            permission on it, or with global audit access. Jobs above are still visible to you.
          </p>
        </div>
      ) : audit.error ? (
        <ErrorState error={audit.error} onRetry={() => audit.refetch()} title="Could not load audit events" />
      ) : events.length === 0 ? (
        <EmptyState icon={<ShieldAlert className="size-5" />} title="No audit events" description="Actions taken on this server will be recorded here." />
      ) : (
        <div className="grid min-w-0 grid-cols-1 gap-3">
          <div className="-mx-4 min-w-0 sm:mx-0">
            <Table className="table-fixed sm:min-w-[720px] sm:table-auto">
              <TableHeader>
                <TableRow>
                  <TableHead className="w-28 pl-4 sm:w-40 sm:pl-2">Time</TableHead>
                  <TableHead className="hidden w-32 sm:table-cell">User</TableHead>
                  <TableHead className="w-24 sm:w-48">Action</TableHead>
                  <TableHead className="pr-4 sm:pr-2">Details</TableHead>
                  <TableHead className="hidden w-28 pr-2 text-right sm:table-cell">Result</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {events.map((e) => {
                  const { scope, verb } = actionParts(e.action);
                  return (
                    <TableRow key={e.id} className="align-top">
                      <TableCell className="pl-4 whitespace-nowrap sm:pl-2">
                        <div className="text-xs" title={formatDateTime(e.created_at)}>
                          {relativeTime(e.created_at, now)}
                        </div>
                        <div className="hidden text-[11px] text-muted-foreground sm:block">{formatDateTime(e.created_at)}</div>
                        <div className="mt-0.5 max-w-28 truncate text-[11px] sm:hidden">{e.actor_username || "system"}</div>
                      </TableCell>
                      <TableCell className="hidden text-xs sm:table-cell">
                        <div className="truncate">{e.actor_username || <span className="text-muted-foreground">system</span>}</div>
                        {e.ip_address && <div className="font-mono text-[11px] text-muted-foreground">{e.ip_address}</div>}
                      </TableCell>
                      <TableCell className="whitespace-normal sm:whitespace-nowrap">
                        <div className="text-xs font-medium">{verb}</div>
                        <div className="font-mono text-[11px] text-muted-foreground" title={e.action}>
                          {scope}
                        </div>
                        {!e.success && <div className="text-[11px] font-medium text-destructive sm:hidden">Failed</div>}
                      </TableCell>
                      <TableCell className="pr-4 whitespace-normal [overflow-wrap:anywhere] sm:max-w-md sm:min-w-32 sm:pr-2">
                        <AuditDetails event={e} />
                      </TableCell>
                      <TableCell className="hidden pr-2 text-right sm:table-cell">
                        <span
                          className={cn(
                            "inline-flex items-center gap-1 text-xs font-medium",
                            e.success ? "text-emerald-700 dark:text-emerald-400" : "text-destructive",
                          )}
                        >
                          {e.success ? <CheckCircle2 className="size-3.5" aria-hidden /> : <XCircle className="size-3.5" aria-hidden />}
                          {e.success ? "Success" : "Failed"}
                        </span>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
          <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>
              {events.length.toLocaleString()} event{events.length === 1 ? "" : "s"} shown
            </span>
            {audit.hasNextPage && (
              <Button variant="outline" size="sm" onClick={() => audit.fetchNextPage()} disabled={audit.isFetchingNextPage}>
                {audit.isFetchingNextPage && <Loader2 className="animate-spin" />}
                Load older events
              </Button>
            )}
          </div>
        </div>
      )}
    </Section>
  );
}

export default function InstanceActivity() {
  const { instance, can } = useInstanceContext();
  const now = useNow(15_000);
  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      <JobsSection key={`jobs-${instance.id}`} instance={instance} now={now} canControl={can("instance.control")} />
      <AuditSection key={`audit-${instance.id}`} instance={instance} now={now} />
    </div>
  );
}
