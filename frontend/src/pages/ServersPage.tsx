import { Archive, FilterX, LayoutGrid, List, Plus, Search, Server, X } from "lucide-react";
import { useMemo } from "react";
import { Link, useSearchParams } from "react-router";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useAuth } from "@/hooks/use-auth";
import { useInstances } from "@/lib/queries";
import type { Instance } from "@/lib/types";
import { ServerCard } from "./library/ServerCard";
import { GroupHeaderButton, ServerTable, type ServerGroup } from "./library/ServerTable";
import {
  loaderLabel,
  searchHaystack,
  serverTypeLabel,
  statusBucket,
  statusRank,
  useStoredState,
  type StatusBucket,
} from "./library/server-utils";

type ViewMode = "grid" | "list";
type StatusFilter = "all" | StatusBucket;
type SortKey = "name" | "status" | "created";

const STATUS_FILTERS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "All statuses" },
  { value: "running", label: "Running" },
  { value: "stopped", label: "Stopped" },
  { value: "busy", label: "Busy" },
  { value: "failed", label: "Failed / unknown" },
];

const SORTS: { value: SortKey; label: string }[] = [
  { value: "name", label: "Name" },
  { value: "status", label: "Status" },
  { value: "created", label: "Recently created" },
];

const UNGROUPED = "";

const isView = (v: unknown): v is ViewMode => v === "grid" || v === "list";
const isStringArray = (v: unknown): v is string[] => Array.isArray(v) && v.every((x) => typeof x === "string");

function matchesSoftware(i: Instance, filter: string): boolean {
  if (filter === "all") return true;
  const sep = filter.indexOf(":");
  const kind = filter.slice(0, sep);
  const value = filter.slice(sep + 1);
  if (kind === "type") return i.server_type === value;
  if (kind === "loader") return i.loader === value;
  return true;
}

function compare(sort: SortKey) {
  const byName = (a: Instance, b: Instance) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
  if (sort === "status") return (a: Instance, b: Instance) => statusRank(a.status) - statusRank(b.status) || byName(a, b);
  if (sort === "created") return (a: Instance, b: Instance) => b.created_at.localeCompare(a.created_at) || byName(a, b);
  return byName;
}

export default function ServersPage() {
  const { can } = useAuth();
  const [params, setParams] = useSearchParams();
  const archived = params.get("archived") === "1";
  const q = params.get("q") ?? "";
  const statusParam = params.get("status") ?? "all";
  const status: StatusFilter = STATUS_FILTERS.some((s) => s.value === statusParam) ? (statusParam as StatusFilter) : "all";
  const software = params.get("software") ?? "all";
  const sortParam = params.get("sort") ?? "name";
  const sort: SortKey = SORTS.some((s) => s.value === sortParam) ? (sortParam as SortKey) : "name";

  const [view, setView] = useStoredState<ViewMode>("mcpanel.servers.view", "grid", isView);
  const [collapsedList, setCollapsedList] = useStoredState<string[]>("mcpanel.servers.collapsedGroups", [], isStringArray);
  const collapsed = useMemo(() => new Set(collapsedList), [collapsedList]);

  const query = useInstances(archived ? "true" : "false");
  const all = useMemo(() => query.data ?? [], [query.data]);

  const setParam = (key: string, value: string, fallback = "") => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (!value || value === fallback) next.delete(key);
        else next.set(key, value);
        return next;
      },
      { replace: true },
    );
  };

  const clearFilters = () =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        ["q", "status", "software"].forEach((k) => next.delete(k));
        return next;
      },
      { replace: true },
    );

  const toggleGroup = (key: string) =>
    setCollapsedList((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));

  // Filter options are derived from the servers actually present.
  const softwareOptions = useMemo(() => {
    const types = [...new Set(all.map((i) => i.server_type))].sort((a, b) =>
      serverTypeLabel(a).localeCompare(serverTypeLabel(b)),
    );
    const loaders = [...new Set(all.map((i) => i.loader).filter(Boolean))].sort();
    // Loaders are only a distinct dimension when modpacks exist (a standard server's loader is its type).
    const showLoaders = all.some((i) => i.is_modpack) && loaders.length > 0;
    return { types, loaders: showLoaders ? loaders : [] };
  }, [all]);

  const statusCounts = useMemo(() => {
    const c: Record<StatusFilter, number> = { all: all.length, running: 0, stopped: 0, busy: 0, failed: 0 };
    for (const i of all) c[statusBucket(i.status)]++;
    return c;
  }, [all]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all
      .filter(
        (i) =>
          (!needle || searchHaystack(i).includes(needle)) &&
          (status === "all" || statusBucket(i.status) === status) &&
          matchesSoftware(i, software),
      )
      .sort(compare(sort));
  }, [all, q, status, software, sort]);

  const groups: ServerGroup[] = useMemo(() => {
    const map = new Map<string, Instance[]>();
    for (const i of filtered) {
      const key = i.group.trim() || UNGROUPED;
      const arr = map.get(key);
      if (arr) arr.push(i);
      else map.set(key, [i]);
    }
    const keys = [...map.keys()]
      .filter((k) => k !== UNGROUPED)
      .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
    if (map.has(UNGROUPED)) keys.push(UNGROUPED);
    return keys.map((key) => ({ key, label: key || "Ungrouped", items: map.get(key)! }));
  }, [filtered]);

  // Group headers only make sense once at least one server has a group.
  const grouped = all.some((i) => i.group.trim());
  const filtersActive = !!q || status !== "all" || software !== "all";
  const canCreate = can("instance.create");

  const createButton = canCreate && (
    <Button size="sm" asChild>
      <Link to="/servers/new">
        <Plus /> Create server
      </Link>
    </Button>
  );

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      <PageHeader
        className="pb-0"
        title={archived ? "Archived servers" : "Servers"}
        description={
          query.data
            ? filtersActive
              ? `Showing ${filtered.length} of ${all.length} server${all.length === 1 ? "" : "s"}`
              : `${all.length} ${archived ? "archived " : ""}server${all.length === 1 ? "" : "s"}${
                  statusCounts.running ? ` · ${statusCounts.running} running` : ""
                }`
            : "Your server library"
        }
        actions={createButton}
      />

      <div className="flex flex-wrap items-center gap-2" role="search">
        <InputGroup className="w-full md:w-64">
          <InputGroupAddon>
            <Search aria-hidden />
          </InputGroupAddon>
          <InputGroupInput
            type="search"
            aria-label="Search servers"
            placeholder="Search name, version, type…"
            value={q}
            onChange={(e) => setParam("q", e.target.value)}
          />
          {q && (
            <InputGroupAddon align="inline-end">
              <InputGroupButton size="icon-xs" aria-label="Clear search" onClick={() => setParam("q", "")}>
                <X />
              </InputGroupButton>
            </InputGroupAddon>
          )}
        </InputGroup>

        <div className="contents">
          <Select value={status} onValueChange={(v) => setParam("status", v, "all")}>
            <SelectTrigger size="sm" aria-label="Filter by status" className="min-w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STATUS_FILTERS.map((s) => (
                <SelectItem key={s.value} value={s.value}>
                  {s.label}
                  {s.value !== "all" && (
                    <span className="text-xs tabular-nums text-muted-foreground">{statusCounts[s.value]}</span>
                  )}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={software} onValueChange={(v) => setParam("software", v, "all")}>
            <SelectTrigger size="sm" aria-label="Filter by server type or loader" className="min-w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All software</SelectItem>
              {softwareOptions.types.length > 0 && (
                <SelectGroup>
                  <SelectLabel>Server type</SelectLabel>
                  {softwareOptions.types.map((t) => (
                    <SelectItem key={t} value={`type:${t}`}>
                      {serverTypeLabel(t)}
                    </SelectItem>
                  ))}
                </SelectGroup>
              )}
              {softwareOptions.loaders.length > 0 && (
                <SelectGroup>
                  <SelectLabel>Loader</SelectLabel>
                  {softwareOptions.loaders.map((l) => (
                    <SelectItem key={l} value={`loader:${l}`}>
                      {loaderLabel(l)}
                    </SelectItem>
                  ))}
                </SelectGroup>
              )}
              {software !== "all" &&
                !softwareOptions.types.some((t) => `type:${t}` === software) &&
                !softwareOptions.loaders.some((l) => `loader:${l}` === software) && (
                  <SelectItem value={software}>{software.split(":")[1] ?? software}</SelectItem>
                )}
            </SelectContent>
          </Select>

          <Select value={sort} onValueChange={(v) => setParam("sort", v, "name")}>
            <SelectTrigger size="sm" aria-label="Sort servers" className="min-w-36">
              <span className="text-muted-foreground">Sort:</span>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SORTS.map((s) => (
                <SelectItem key={s.value} value={s.value}>
                  {s.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <div className="flex items-center gap-2 px-1">
            <Switch id="servers-archived" checked={archived} onCheckedChange={(v) => setParam("archived", v ? "1" : "")} />
            <Label htmlFor="servers-archived" className="text-[13px] font-normal">
              Archived
            </Label>
          </div>

          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            spacing={0}
            value={view}
            onValueChange={(v) => isView(v) && setView(v)}
            aria-label="Layout"
            className="ml-auto"
          >
            <ToggleGroupItem value="grid" aria-label="Grid view">
              <LayoutGrid />
            </ToggleGroupItem>
            <ToggleGroupItem value="list" aria-label="List view">
              <List />
            </ToggleGroupItem>
          </ToggleGroup>
        </div>
      </div>

      {query.isLoading ? (
        <LoadingState label="Loading servers…" />
      ) : query.error ? (
        <ErrorState error={query.error} onRetry={() => query.refetch()} title="Could not load servers" />
      ) : all.length === 0 ? (
        archived ? (
          <EmptyState
            icon={<Archive className="size-5" />}
            title="No archived servers"
            description="Archived servers keep their files but cannot be started. Archive a server from its Settings tab."
            action={
              <Button size="sm" variant="outline" onClick={() => setParam("archived", "")}>
                Show active servers
              </Button>
            }
          />
        ) : (
          <EmptyState
            icon={<Server className="size-5" />}
            title="No servers yet"
            description={
              canCreate
                ? "Create a server from a version, a loader such as Fabric or Paper, or a modpack."
                : "You do not have access to any servers yet. Ask an administrator to grant you access."
            }
            action={createButton}
          />
        )
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={<FilterX className="size-5" />}
          title="No servers match"
          description="Try a different search term or clear the filters."
          action={
            <Button size="sm" variant="outline" onClick={clearFilters}>
              <FilterX /> Clear filters
            </Button>
          }
        />
      ) : view === "list" ? (
        <ServerTable groups={groups} grouped={grouped} collapsed={collapsed} onToggle={toggleGroup} />
      ) : grouped ? (
        <div className="grid gap-5">
          {groups.map((g) => {
            const isCollapsed = collapsed.has(g.key);
            const panelId = `servers-group-${encodeURIComponent(g.key || "ungrouped")}`;
            return (
              <section key={g.key} aria-label={g.label} className="grid gap-2.5">
                <GroupHeaderButton
                  label={g.label}
                  items={g.items}
                  collapsed={isCollapsed}
                  onToggle={() => toggleGroup(g.key)}
                  controls={panelId}
                  className="w-fit max-w-full px-1 py-1"
                />
                {!isCollapsed && (
                  <div id={panelId}>
                    <CardGrid items={g.items} />
                  </div>
                )}
              </section>
            );
          })}
        </div>
      ) : (
        <CardGrid items={filtered} />
      )}
    </div>
  );
}

function CardGrid({ items }: { items: Instance[] }) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
      {items.map((i) => (
        <ServerCard key={i.id} instance={i} />
      ))}
    </div>
  );
}
