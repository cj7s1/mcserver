/**
 * Players page of games other than Minecraft (backend contract B9): online players, and the game's
 * whitelist / admin / ban lists with the labels the API gives them ({ops: "Admins"} for Factorio).
 * Names are checked with the game's pattern; no avatars or UUIDs (those are Minecraft's).
 */
import { Ban, Loader2, Plus, Trash2, UserX } from "lucide-react";
import { useId, useState, type FormEvent } from "react";
import { Section } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { gameLabel } from "@/lib/games";
import type { Instance, PlayersResponse } from "@/lib/types";
import { useGameInfo } from "../game-api";
import { ACTIONS, gamePlayerNameError, listLabels, type ListKey } from "./game-players-model";
import { usePlayerAction, usePlayers } from "./player-utils";

function AddForm({ instanceId, list, label, pattern, disabled }: { instanceId: string; list: ListKey; label: string; pattern?: string; disabled: boolean }) {
  const id = useId();
  const act = usePlayerAction(instanceId);
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const [touched, setTouched] = useState(false);
  const error = touched ? gamePlayerNameError(name, pattern) : null;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (gamePlayerNameError(name, pattern)) return;
    act.mutate(
      { action: ACTIONS[list].add, name, reason: list === "bans" ? reason : undefined },
      {
        onSuccess: () => {
          setName("");
          setReason("");
          setTouched(false);
        },
      },
    );
  };
  return (
    <form onSubmit={submit} className="grid gap-2">
      <div className="flex flex-wrap items-end gap-2">
        <div className="grid min-w-40 flex-1 gap-1">
          <Label htmlFor={`${id}-name`}>Player name</Label>
          <Input
            id={`${id}-name`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={disabled || act.isPending}
            aria-invalid={!!error || undefined}
            aria-label={`Player name to add to ${label}`}
            autoComplete="off"
            spellCheck={false}
          />
        </div>
        {list === "bans" && (
          <div className="grid min-w-40 flex-1 gap-1">
            <Label htmlFor={`${id}-reason`}>Reason (optional)</Label>
            <Input id={`${id}-reason`} value={reason} onChange={(e) => setReason(e.target.value)} disabled={disabled || act.isPending} maxLength={200} />
          </div>
        )}
        <Button type="submit" size="sm" disabled={disabled || act.isPending}>
          {act.isPending ? <Loader2 className="animate-spin" /> : list === "bans" ? <Ban /> : <Plus />} {ACTIONS[list].addLabel}
        </Button>
      </div>
      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}

function PlayerList({
  instanceId,
  list,
  label,
  entries,
  canManage,
  pattern,
}: {
  instanceId: string;
  list: ListKey;
  label: string;
  entries: { name: string; reason?: string }[];
  canManage: boolean;
  pattern?: string;
}) {
  const act = usePlayerAction(instanceId);
  return (
    <Section title={label} description={`${entries.length} ${entries.length === 1 ? "entry" : "entries"}`}>
      <div className="grid gap-3">
        {entries.length ? (
          <ul className="divide-y rounded-lg border" aria-label={label}>
            {entries.map((e) => (
              <li key={e.name} className="flex items-center gap-2 px-3 py-2 text-sm">
                <span className="min-w-0 truncate font-medium">{e.name}</span>
                {e.reason && <span className="min-w-0 truncate text-xs text-muted-foreground">{e.reason}</span>}
                {canManage && (
                  <Button
                    size="xs"
                    variant="ghost"
                    className="ml-auto"
                    onClick={() => act.mutate({ action: ACTIONS[list].remove, name: e.name })}
                    disabled={act.isPending}
                    aria-label={`${ACTIONS[list].removeLabel} ${e.name}`}
                  >
                    <Trash2 /> {ACTIONS[list].removeLabel}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">Nobody yet.</p>
        )}
        {canManage && <AddForm instanceId={instanceId} list={list} label={label} pattern={pattern} disabled={false} />}
      </div>
    </Section>
  );
}

export function GamePlayers({ instance, canManage }: { instance: Instance; canManage: boolean }) {
  const running = ["running", "starting", "unhealthy", "restarting"].includes(instance.status);
  const query = usePlayers(instance.id, running);
  const game = useGameInfo(instance);
  const kick = usePlayerAction(instance.id);
  if (query.isLoading) return <LoadingState label="Loading players…" />;
  if (query.error || !query.data) return <ErrorState error={query.error ?? new Error("No data")} title="Could not load players" onRetry={() => query.refetch()} />;
  const data = query.data as unknown as PlayersResponse;
  const labels = listLabels(data, game?.player_list_labels);
  const pattern = game?.player_name_pattern;
  const online = data.online;
  const lists: ListKey[] = ["whitelist", "ops", "bans"];
  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      <p className="rounded-lg border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        {data.running
          ? `The server is running: changes are sent to it as console commands and apply at once; ${gameLabel(instance)} saves them to its lists itself.`
          : "The server is stopped: changes are written to its list files and apply when it starts."}
      </p>
      <Section
        title="Online"
        description={
          !data.running
            ? "The server is not running."
            : online?.error
              ? online.error
              : `${online?.online ?? online?.players.length ?? 0}${online?.max ? ` of ${online.max}` : ""} online`
        }
      >
        {data.running && online?.players.length ? (
          <ul className="divide-y rounded-lg border" aria-label="Online players">
            {online.players.map((p) => (
              <li key={p} className="flex items-center gap-2 px-3 py-2 text-sm">
                <span className="font-medium">{p}</span>
                {canManage && (
                  <Button size="xs" variant="ghost" className="ml-auto" onClick={() => kick.mutate({ action: "kick", name: p })} disabled={kick.isPending} aria-label={`Kick ${p}`}>
                    <UserX /> Kick
                  </Button>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">Nobody is online.</p>
        )}
      </Section>
      <div className="grid gap-4 lg:grid-cols-3">
        {lists.map((l) => (
          <PlayerList
            key={l}
            instanceId={instance.id}
            list={l}
            label={labels[l]}
            entries={(data[l] ?? []).map((e) => ({ name: e.name, reason: e.reason }))}
            canManage={canManage}
            pattern={pattern}
          />
        ))}
      </div>
    </div>
  );
}
