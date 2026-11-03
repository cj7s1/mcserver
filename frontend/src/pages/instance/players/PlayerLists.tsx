/** Whitelist / operators / bans lists with add-by-name forms. */
import { Ban, Gavel, ListChecks, Loader2, Plus, Search, ShieldCheck, Trash2, Undo2 } from "lucide-react";
import { useId, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { EmptyState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ConfirmAction } from "../content/ConfirmAction";
import { PlayerAvatar } from "./PlayerAvatar";
import {
  formatMcDate,
  playerNameError,
  sameName,
  usePlayerAction,
  type McListEntry,
  type McPlayers,
  type PlayerAction,
} from "./player-utils";

interface ListEnv {
  instanceId: string;
  data: McPlayers;
  canManage: boolean;
  showHeads: boolean;
}

const OP_LEVELS: Record<number, string> = {
  1: "Can bypass spawn protection",
  2: "Can use cheat commands and command blocks",
  3: "Can kick, ban and op players",
  4: "Can use every command, including /stop",
};

function AddPlayerForm({
  env,
  action,
  submitLabel,
  submitIcon,
  existing,
  duplicateText,
  withReason = false,
  help,
}: {
  env: ListEnv;
  action: PlayerAction;
  submitLabel: string;
  submitIcon: ReactNode;
  existing: McListEntry[];
  duplicateText: string;
  withReason?: boolean;
  help: ReactNode;
}) {
  const id = useId();
  const act = usePlayerAction(env.instanceId);
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const trimmed = name.trim();
  const error =
    playerNameError(name) ?? (existing.some((e) => sameName(e.name, trimmed)) ? duplicateText.replace("{name}", trimmed) : null);
  const visibleError = name !== "" || submitted ? error : null;
  const suggestions = useMemo(() => {
    const names = new Set<string>();
    for (const n of env.data.online?.players ?? []) names.add(n);
    for (const k of env.data.known_players) if (k.name) names.add(k.name);
    return [...names].filter((n) => !existing.some((e) => sameName(e.name, n))).slice(0, 200);
  }, [env.data, existing]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (error) return;
    act.mutate(
      { action, name: trimmed, reason: withReason ? reason : undefined },
      {
        onSuccess: () => {
          setName("");
          setReason("");
          setSubmitted(false);
        },
      },
    );
  };

  return (
    <form onSubmit={submit} className="grid gap-2" noValidate>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start">
        <div className="grid gap-1 sm:w-56">
          <Label htmlFor={`${id}-name`}>Player name</Label>
          <Input
            id={`${id}-name`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={32}
            autoComplete="off"
            spellCheck={false}
            list={suggestions.length ? `${id}-suggestions` : undefined}
            aria-invalid={!!visibleError}
            aria-describedby={`${id}-help`}
            placeholder="e.g. Notch"
          />
          {suggestions.length > 0 && (
            <datalist id={`${id}-suggestions`}>
              {suggestions.map((n) => (
                <option key={n} value={n} />
              ))}
            </datalist>
          )}
        </div>
        {withReason && (
          <div className="grid flex-1 gap-1">
            <Label htmlFor={`${id}-reason`}>Reason (optional)</Label>
            <Input
              id={`${id}-reason`}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={200}
              autoComplete="off"
              placeholder="Shown to the player"
            />
          </div>
        )}
        <Button type="submit" className="sm:mt-[1.375rem]" disabled={act.isPending} variant={action === "ban" ? "destructive" : "default"}>
          {act.isPending ? <Loader2 className="animate-spin" /> : submitIcon}
          {submitLabel}
        </Button>
      </div>
      <p id={`${id}-help`} className={visibleError ? "text-xs text-destructive" : "text-xs text-muted-foreground"} aria-live="polite">
        {visibleError ?? help}
      </p>
    </form>
  );
}

function PlayerCell({ entry, env }: { entry: McListEntry; env: ListEnv }) {
  const name = entry.name || "(unknown name)";
  const online = (env.data.online?.players ?? []).some((n) => sameName(n, entry.name));
  return (
    <div className="flex min-w-0 items-center gap-2">
      <PlayerAvatar name={name} uuid={entry.uuid} showImage={env.showHeads} />
      <div className="min-w-0">
        <p className="flex items-center gap-1.5 truncate font-medium">
          {name}
          {online && (
            <span className="inline-flex items-center gap-1 text-[11px] font-normal text-emerald-700 dark:text-emerald-300">
              <span className="size-1.5 rounded-full bg-emerald-500" aria-hidden /> online
            </span>
          )}
        </p>
        {entry.uuid && (
          <p className="truncate font-mono text-[11px] text-muted-foreground sm:hidden" title={entry.uuid}>
            {entry.uuid}
          </p>
        )}
      </div>
    </div>
  );
}

function UuidCell({ uuid }: { uuid?: string }) {
  return (
    <TableCell className="hidden font-mono text-[11px] text-muted-foreground sm:table-cell" title={uuid}>
      {uuid ?? "—"}
    </TableCell>
  );
}

function RemoveButton({
  env,
  entry,
  action,
  label,
  confirm,
}: {
  env: ListEnv;
  entry: McListEntry;
  action: PlayerAction;
  label: string;
  confirm?: { title: string; description: string; confirmLabel: string };
}) {
  const act = usePlayerAction(env.instanceId);
  const name = entry.name ?? "";
  if (!env.canManage || !name) return null;
  const icon = act.isPending ? <Loader2 className="animate-spin" /> : action === "pardon" ? <Undo2 /> : <Trash2 />;
  if (!confirm) {
    return (
      <Button size="xs" variant="ghost" disabled={act.isPending} onClick={() => act.mutate({ action, name })}>
        {icon} {label}
      </Button>
    );
  }
  return (
    <ConfirmAction
      trigger={
        <Button size="xs" variant="ghost" disabled={act.isPending} aria-label={`${label}: ${name}`}>
          {icon} <span className="hidden sm:inline">{label}</span>
        </Button>
      }
      title={confirm.title}
      description={confirm.description}
      confirmLabel={confirm.confirmLabel}
      destructive
      onConfirm={() => act.mutateAsync({ action, name })}
    />
  );
}

function useFiltered(entries: McListEntry[], text: string) {
  return useMemo(() => {
    const needle = text.trim().toLowerCase();
    const sorted = [...entries].sort((a, b) => (a.name ?? a.ip ?? "").localeCompare(b.name ?? b.ip ?? "", undefined, { sensitivity: "base" }));
    if (!needle) return sorted;
    return sorted.filter((e) =>
      [e.name, e.uuid, e.ip, e.reason].some((v) => (v ?? "").toLowerCase().includes(needle)),
    );
  }, [entries, text]);
}

function ListFilter({ value, onChange, label, show }: { value: string; onChange: (v: string) => void; label: string; show: boolean }) {
  const id = useId();
  if (!show) return null;
  return (
    <div className="relative sm:w-64">
      <Label htmlFor={id} className="sr-only">
        {label}
      </Label>
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
      <Input id={id} type="search" value={value} onChange={(e) => onChange(e.target.value)} placeholder={label} className="pl-8" />
    </div>
  );
}

function WhitelistTab({ env }: { env: ListEnv }) {
  const [text, setText] = useState("");
  const rows = useFiltered(env.data.whitelist, text);
  return (
    <div className="grid grid-cols-1 gap-4">
      {env.canManage && (
        <AddPlayerForm
          env={env}
          action="whitelist_add"
          submitLabel="Add to whitelist"
          submitIcon={<Plus />}
          existing={env.data.whitelist}
          duplicateText="{name} is already on the whitelist"
          help="Java Edition name. The UUID is looked up with Mojang (or derived from the name on offline-mode servers)."
        />
      )}
      <ListFilter value={text} onChange={setText} label="Filter whitelist" show={env.data.whitelist.length > 8} />
      {env.data.whitelist.length === 0 ? (
        <EmptyState icon={<ListChecks className="size-5" />} title="The whitelist is empty" className="py-8" />
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Player</TableHead>
                <TableHead className="hidden sm:table-cell">UUID</TableHead>
                <TableHead className="text-right">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((e) => (
                <TableRow key={e.uuid ?? e.name}>
                  <TableCell className="max-w-0 min-w-40 py-2">
                    <PlayerCell entry={e} env={env} />
                  </TableCell>
                  <UuidCell uuid={e.uuid} />
                  <TableCell className="py-2 text-right">
                    <RemoveButton
                      env={env}
                      entry={e}
                      action="whitelist_remove"
                      label="Remove"
                      confirm={{
                        title: `Remove ${e.name} from the whitelist?`,
                        description: "While the whitelist is on, they can no longer join. Players already online stay connected.",
                        confirmLabel: "Remove",
                      }}
                    />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

function OpsTab({ env }: { env: ListEnv }) {
  const [text, setText] = useState("");
  const rows = useFiltered(env.data.ops, text);
  return (
    <div className="grid grid-cols-1 gap-4">
      {env.canManage && (
        <AddPlayerForm
          env={env}
          action="op"
          submitLabel="Make operator"
          submitIcon={<ShieldCheck />}
          existing={env.data.ops}
          duplicateText="{name} is already an operator"
          help={
            env.data.running
              ? "Operators get the level set by op-permission-level in server.properties (4 by default): full control of the server."
              : "Operators added while the server is stopped get level 4: they can use every command, including /stop."
          }
        />
      )}
      <ListFilter value={text} onChange={setText} label="Filter operators" show={env.data.ops.length > 8} />
      {env.data.ops.length === 0 ? (
        <EmptyState icon={<ShieldCheck className="size-5" />} title="No operators" className="py-8" />
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Player</TableHead>
                <TableHead className="hidden sm:table-cell">UUID</TableHead>
                <TableHead>Level</TableHead>
                <TableHead className="hidden md:table-cell">Bypasses player limit</TableHead>
                <TableHead className="text-right">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((e) => (
                <TableRow key={e.uuid ?? e.name}>
                  <TableCell className="max-w-0 min-w-40 py-2">
                    <PlayerCell entry={e} env={env} />
                  </TableCell>
                  <UuidCell uuid={e.uuid} />
                  <TableCell className="py-2">
                    {e.level != null ? (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span
                            tabIndex={0}
                            className="inline-flex h-5 items-center rounded-full bg-amber-500/10 px-2 text-xs font-medium text-amber-700 outline-none focus-visible:ring-2 focus-visible:ring-ring dark:text-amber-300"
                          >
                            Level {e.level}
                          </span>
                        </TooltipTrigger>
                        <TooltipContent>{OP_LEVELS[e.level] ?? "Custom level"}</TooltipContent>
                      </Tooltip>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell className="hidden py-2 text-xs text-muted-foreground md:table-cell">
                    {e.bypassesPlayerLimit ? "Yes" : "No"}
                  </TableCell>
                  <TableCell className="py-2 text-right">
                    <RemoveButton
                      env={env}
                      entry={e}
                      action="deop"
                      label="Remove operator"
                      confirm={{
                        title: `Remove operator status from ${e.name}?`,
                        description: "They stay on the server as a regular player and can no longer use operator commands.",
                        confirmLabel: "Remove operator",
                      }}
                    />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

function BanDetails({ e }: { e: McListEntry }) {
  return (
    <>
      <TableCell className="max-w-56 py-2 text-xs">
        <span className="line-clamp-2" title={e.reason}>
          {e.reason || <span className="text-muted-foreground">No reason</span>}
        </span>
      </TableCell>
      <TableCell className="hidden py-2 text-xs text-muted-foreground md:table-cell">
        {formatMcDate(e.created)}
        {e.source && <span className="block truncate">by {e.source}</span>}
      </TableCell>
      <TableCell className="hidden py-2 text-xs text-muted-foreground lg:table-cell">{formatMcDate(e.expires)}</TableCell>
    </>
  );
}

function BansTab({ env }: { env: ListEnv }) {
  const [text, setText] = useState("");
  const rows = useFiltered(env.data.bans, text);
  const ipRows = useFiltered(env.data.ip_bans, text);
  return (
    <div className="grid grid-cols-1 gap-4">
      {env.canManage && (
        <AddPlayerForm
          env={env}
          action="ban"
          submitLabel="Ban player"
          submitIcon={<Gavel />}
          existing={env.data.bans}
          duplicateText="{name} is already banned"
          withReason
          help="Bans are permanent until lifted here. A banned player who is online is disconnected."
        />
      )}
      <ListFilter value={text} onChange={setText} label="Filter bans" show={env.data.bans.length + env.data.ip_bans.length > 8} />
      {env.data.bans.length === 0 ? (
        <EmptyState icon={<Ban className="size-5" />} title="No banned players" className="py-8" />
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Player</TableHead>
                <TableHead>Reason</TableHead>
                <TableHead className="hidden md:table-cell">Banned</TableHead>
                <TableHead className="hidden lg:table-cell">Expires</TableHead>
                <TableHead className="text-right">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((e) => (
                <TableRow key={e.uuid ?? e.name}>
                  <TableCell className="max-w-0 min-w-40 py-2">
                    <PlayerCell entry={e} env={env} />
                  </TableCell>
                  <BanDetails e={e} />
                  <TableCell className="py-2 text-right">
                    <RemoveButton env={env} entry={e} action="pardon" label="Unban" />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {env.data.ip_bans.length > 0 && (
        <div className="grid gap-2">
          <div>
            <h3 className="text-sm font-semibold">IP bans</h3>
            <p className="text-xs text-muted-foreground">
              Read-only here. Lift an IP ban with the console command <span className="font-mono">pardon-ip &lt;address&gt;</span>.
            </p>
          </div>
          <div className="rounded-lg border">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>Address</TableHead>
                  <TableHead>Reason</TableHead>
                  <TableHead className="hidden md:table-cell">Banned</TableHead>
                  <TableHead className="hidden lg:table-cell">Expires</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {ipRows.map((e) => (
                  <TableRow key={e.ip}>
                    <TableCell className="py-2 font-mono text-xs">{e.ip ?? "—"}</TableCell>
                    <BanDetails e={e} />
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}
    </div>
  );
}

function Count({ n }: { n: number }) {
  return <span className="rounded-full bg-muted-foreground/15 px-1.5 text-[11px] tabular-nums">{n}</span>;
}

export function PlayerLists(env: ListEnv) {
  const [tab, setTab] = useState("whitelist");
  return (
    <Tabs value={tab} onValueChange={setTab}>
      <TabsList aria-label="Player lists" className="w-full sm:w-fit">
        <TabsTrigger value="whitelist" className="px-3">
          Whitelist <Count n={env.data.whitelist.length} />
        </TabsTrigger>
        <TabsTrigger value="ops" className="px-3">
          Operators <Count n={env.data.ops.length} />
        </TabsTrigger>
        <TabsTrigger value="bans" className="px-3">
          Bans <Count n={env.data.bans.length + env.data.ip_bans.length} />
        </TabsTrigger>
      </TabsList>
      <TabsContent value="whitelist" className="mt-3">
        <WhitelistTab env={env} />
      </TabsContent>
      <TabsContent value="ops" className="mt-3">
        <OpsTab env={env} />
      </TabsContent>
      <TabsContent value="bans" className="mt-3">
        <BansTab env={env} />
      </TabsContent>
    </Tabs>
  );
}
