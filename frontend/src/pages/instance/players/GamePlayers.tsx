/**
 * Players page of games other than Minecraft (backend contract B9): online players, and the game's
 * whitelist / admin / ban lists with the labels the API gives them ({ops: "Admins"} for Factorio).
 * Names are checked with the game's pattern; no avatars or UUIDs (those are Minecraft's).
 *
 * What the answer may add (the six Steam-era games): online `entries` with the id kicks and bans use
 * (Palworld, Rust); entries whose `name` is an id and `display_name` the player's name (Rust); `ip_bans`
 * (HKMP, lifted by address); `id_hint` (what a typed name may be: Valheim takes SteamID64s);
 * `no_online_list` (Valheim cannot list who is online); `lists_unknown` (Project Zomboid keeps its lists in
 * its database: changed by name, never listed); `changes_while_stopped` (false: the game changes its lists
 * only while it runs, the note says so).
 *
 * A typed display name the game cannot resolve to one player without guessing (Rust, Palworld: 409
 * `ambiguous_player`) opens PlayerChoiceDialog, which sends the action again with the chosen id (C-5).
 */
import { Ban, Loader2, Plus, Terminal, Trash2, UserX } from "lucide-react";
import { useId, useState, type FormEvent } from "react";
import { Section } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { gameLabel } from "@/lib/games";
import type { Instance, OnlinePlayerEntry, PlayersResponse } from "@/lib/types";
import { ConfirmAction } from "../content/ConfirmAction";
import { useGameInfo } from "../game-api";
import { ACTIONS, gamePlayerNameError, listEntries, listLabels, type GameListEntry, type ListKey } from "./game-players-model";
import { ConsoleOnlyNames } from "./ConsoleOnlyNames";
import { PlayerChoiceDialog } from "./PlayerChoiceDialog";
import { ambiguousCandidates, usePlayerAction, usePlayers, type AmbiguousPlayer } from "./player-utils";

/** A player action whose 409 `ambiguous_player` opens the choice of the player (rendered as `dialog`). */
function useChoosingPlayerAction(instanceId: string, onDone?: () => void) {
  const [choice, setChoice] = useState<AmbiguousPlayer | null>(null);
  const act = usePlayerAction(instanceId, { onAmbiguous: setChoice });
  const dialog = (
    <PlayerChoiceDialog
      choice={choice}
      onClose={() => setChoice(null)}
      onPick={(c) => choice && act.mutate({ ...choice.vars, name: c.id }, { onSuccess: () => onDone?.() })}
    />
  );
  return { act, dialog };
}

function AddForm({
  instanceId,
  list,
  label,
  pattern,
  disabled,
  hint,
  withRemove,
}: {
  instanceId: string;
  list: ListKey;
  label: string;
  pattern?: string;
  disabled: boolean;
  /** What a typed name may be (the answer's `id_hint`). */
  hint?: string;
  /** Also a "remove by name" button (a list the panel cannot read: Project Zomboid). */
  withRemove?: boolean;
}) {
  const id = useId();
  const [name, setName] = useState("");
  const [reason, setReason] = useState("");
  const [touched, setTouched] = useState(false);
  const clear = () => {
    setName("");
    setReason("");
    setTouched(false);
  };
  const { act, dialog } = useChoosingPlayerAction(instanceId, clear);
  const error = touched ? gamePlayerNameError(name, pattern) : null;
  const run = (action: "add" | "remove") => {
    setTouched(true);
    if (gamePlayerNameError(name, pattern)) return;
    act.mutate({ action: ACTIONS[list][action], name, reason: list === "bans" && action === "add" ? reason : undefined }, { onSuccess: clear });
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    run("add");
  };
  return (
    <form onSubmit={submit} className="grid gap-2">
      <div className="flex flex-wrap items-end gap-2">
        <div className="grid min-w-40 flex-1 gap-1">
          <Label htmlFor={`${id}-name`}>{hint ? "Player" : "Player name"}</Label>
          <Input
            id={`${id}-name`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={disabled || act.isPending}
            aria-invalid={!!error || undefined}
            aria-label={`Player name to add to ${label}`}
            aria-describedby={hint && !error ? `${id}-hint` : undefined}
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
        {withRemove && (
          <Button type="button" size="sm" variant="outline" disabled={disabled || act.isPending} onClick={() => run("remove")}>
            <Trash2 /> {ACTIONS[list].removeLabel}
          </Button>
        )}
      </div>
      {error ? (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {dialog}
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
  hint,
  unknown,
}: {
  instanceId: string;
  list: ListKey;
  label: string;
  entries: GameListEntry[];
  canManage: boolean;
  pattern?: string;
  hint?: string;
  /** The game keeps this list where the panel cannot read it (Project Zomboid's database). */
  unknown?: boolean;
}) {
  const { act, dialog } = useChoosingPlayerAction(instanceId);
  return (
    <Section title={label} description={unknown ? "Not readable by the panel" : `${entries.length} ${entries.length === 1 ? "entry" : "entries"}`}>
      <div className="grid gap-3">
        {entries.length ? (
          <ul className="divide-y rounded-lg border" aria-label={label}>
            {entries.map((e) => (
              <li key={`${e.ip ? "ip" : "name"}:${e.name}`} className="flex min-w-0 items-center gap-2 px-3 py-2 text-sm">
                <span className="grid min-w-0 gap-0.5">
                  <span className="truncate font-medium">{e.display_name || e.name}</span>
                  {(e.display_name || e.ip) && (
                    <span className="truncate font-mono text-[11px] text-muted-foreground">{e.ip ? `IP address ${e.name}` : e.name}</span>
                  )}
                </span>
                {e.reason && <span className="min-w-0 truncate text-xs text-muted-foreground">{e.reason}</span>}
                {canManage && (
                  <Button
                    size="xs"
                    variant="ghost"
                    className="ml-auto"
                    onClick={() => act.mutate({ action: ACTIONS[list].remove, name: e.name })}
                    disabled={act.isPending}
                    aria-label={`${ACTIONS[list].removeLabel} ${e.display_name || e.name}`}
                  >
                    <Trash2 /> {ACTIONS[list].removeLabel}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">
            {unknown ? "The game keeps this list itself: add or remove players by name while the server runs." : "Nobody yet."}
          </p>
        )}
        {canManage && (
          <AddForm instanceId={instanceId} list={list} label={label} pattern={pattern} disabled={false} hint={hint} withRemove={unknown} />
        )}
        {dialog}
      </div>
    </Section>
  );
}

/** An online player with the id the game kicks and bans by (Palworld, Rust): kick, and ban when the game has a ban list. */
function OnlineEntry({ instanceId, entry, canManage, canBan }: { instanceId: string; entry: OnlinePlayerEntry; canManage: boolean; canBan: boolean }) {
  const { act, dialog } = useChoosingPlayerAction(instanceId);
  const target = entry.id || entry.name;
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-2 px-3 py-2 text-sm">
      <span className="grid min-w-0 gap-0.5">
        <span className="truncate font-medium">{entry.name}</span>
        {entry.id && <span className="truncate font-mono text-[11px] text-muted-foreground">{entry.id}</span>}
      </span>
      {canManage && (
        <span className="ml-auto flex items-center gap-1">
          <Button size="xs" variant="ghost" onClick={() => act.mutate({ action: "kick", name: target })} disabled={act.isPending} aria-label={`Kick ${entry.name}`}>
            <UserX /> Kick
          </Button>
          {canBan && (
            <ConfirmAction
              trigger={
                <Button size="xs" variant="ghost" className="text-destructive hover:text-destructive" disabled={act.isPending} aria-label={`Ban ${entry.name}`}>
                  <Ban /> Ban
                </Button>
              }
              title={`Ban ${entry.name}?`}
              description={`They are disconnected and cannot join again until they are unbanned${entry.id ? ` (banned by their id ${entry.id})` : ""}.`}
              confirmLabel="Ban player"
              destructive
              onConfirm={() =>
                // a name the panel would not guess closes this dialog and opens the choice of the player
                act.mutateAsync({ action: "ban", name: target }).catch((err: unknown) => {
                  if (!ambiguousCandidates(err)) throw err;
                })
              }
            />
          )}
        </span>
      )}
      {dialog}
    </li>
  );
}

/** The line above the lists: how changes reach the game. */
function modeNote(data: PlayersResponse, game: string): string {
  if (data.no_online_list) return "These lists are files the server reads when it starts: changes are written to them now and apply at the latest at its next start.";
  if (data.lists_unknown) return data.id_hint || `${game} keeps its lists itself: the panel changes them by name while the server runs, but cannot show them.`;
  if (!data.running && data.changes_while_stopped === false)
    return `The server is stopped: ${game} changes these lists only while it runs, so start it to change them.`;
  return data.running
    ? `The server is running: changes are sent to it and apply at once; ${game} saves them to its lists itself.`
    : "The server is stopped: changes are written to its list files and apply when it starts (a game that changes its lists only through the running server refuses them until it runs).";
}

export function GamePlayers({ instance, canManage }: { instance: Instance; canManage: boolean }) {
  const running = ["running", "starting", "unhealthy", "restarting"].includes(instance.status);
  const query = usePlayers(instance.id, running);
  const game = useGameInfo(instance);
  const { act: kick, dialog: kickChoice } = useChoosingPlayerAction(instance.id);
  if (query.isLoading) return <LoadingState label="Loading players…" />;
  if (query.error || !query.data) return <ErrorState error={query.error ?? new Error("No data")} title="Could not load players" onRetry={() => query.refetch()} />;
  const data = query.data as unknown as PlayersResponse;
  const labels = listLabels(data, game?.player_list_labels);
  const pattern = game?.player_name_pattern;
  const online = data.online;
  // only the lists the game has: an answer with labels names them (Terraria: bans only; TShock, CS2: none)
  const lists: ListKey[] = (["whitelist", "ops", "bans"] as ListKey[]).filter((l) => !data.labels || l in data.labels);
  // a hint about what a typed name may be goes with the lists, not with the online players
  const hint = data.lists_unknown ? undefined : data.id_hint;
  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      {lists.length > 0 && <p className="rounded-lg border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">{modeNote(data, gameLabel(instance))}</p>}
      <Section
        title="Online"
        description={
          data.no_online_list
            ? `${gameLabel(instance)} has no way to list the players online.`
            : !data.running
              ? "The server is not running."
              : online?.error
                ? online.error
                : `${online?.online ?? online?.players.length ?? 0}${online?.max ? ` of ${online.max}` : ""} online`
        }
      >
        {data.no_online_list ? (
          <p className="text-sm text-muted-foreground">Players who are online are not shown here; the lists below still apply.</p>
        ) : data.running && online?.entries?.length ? (
          <ul className="divide-y rounded-lg border" aria-label="Online players">
            {online.entries.map((e) => (
              <OnlineEntry key={`${e.id}:${e.name}`} instanceId={instance.id} entry={e} canManage={canManage} canBan={lists.includes("bans")} />
            ))}
          </ul>
        ) : data.running && online?.players.length ? (
          <ul className="divide-y rounded-lg border" aria-label="Online players">
            {online.players.map((p) => (
              <li key={p} className="flex items-center gap-2 px-3 py-2 text-sm">
                <span className="font-medium">{p}</span>
                {canManage &&
                  // a name the game's commands cannot carry (CS2: quotes, ";"; Project Zomboid: other characters) is
                  // refused by the panel: say where it can be dealt with instead of offering a Kick that fails (C-6)
                  (pattern && gamePlayerNameError(p, pattern) ? (
                    <span className="ml-auto flex shrink-0 items-center gap-1 text-xs text-muted-foreground" title="The panel cannot act on this name">
                      <Terminal className="size-3.5" aria-hidden /> use the console
                    </span>
                  ) : (
                    <Button size="xs" variant="ghost" className="ml-auto" onClick={() => kick.mutate({ action: "kick", name: p })} disabled={kick.isPending} aria-label={`Kick ${p}`}>
                      <UserX /> Kick
                    </Button>
                  ))}
              </li>
            ))}
          </ul>
        ) : !online?.other_names?.length ? (
          <p className="text-sm text-muted-foreground">Nobody is online.</p>
        ) : null}
        {data.running && !data.no_online_list && (
          <ConsoleOnlyNames instanceId={instance.id} names={online?.other_names ?? []} className="mt-3" />
        )}
        {kickChoice}
      </Section>
      {lists.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-3">
          {lists.map((l) => (
            <PlayerList
              key={l}
              instanceId={instance.id}
              list={l}
              label={labels[l]}
              entries={listEntries(data, l)}
              canManage={canManage}
              pattern={pattern}
              hint={hint}
              unknown={data.lists_unknown}
            />
          ))}
        </div>
      )}
    </div>
  );
}
