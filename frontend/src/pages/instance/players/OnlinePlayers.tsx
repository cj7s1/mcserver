/** Players currently connected (via RCON `list`), with quick moderation actions. */
import { Ban, LogOut, ShieldCheck, ShieldOff, WifiOff } from "lucide-react";
import { useState } from "react";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ConfirmAction } from "../content/ConfirmAction";
import { ConsoleOnlyNames } from "./ConsoleOnlyNames";
import { PlayerAvatar } from "./PlayerAvatar";
import { sameName, usePlayerAction, type McPlayers } from "./player-utils";

function ReasonField({ id, value, onChange }: { id: string; value: string; onChange: (v: string) => void }) {
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>Reason (optional, shown to the player)</Label>
      <Input id={id} value={value} onChange={(e) => onChange(e.target.value)} maxLength={200} autoComplete="off" />
    </div>
  );
}

function OnlineRow({
  instanceId,
  name,
  uuid,
  isOp,
  canManage,
  showHeads,
}: {
  instanceId: string;
  name: string;
  uuid?: string;
  isOp: boolean;
  canManage: boolean;
  showHeads: boolean;
}) {
  const act = usePlayerAction(instanceId);
  const [kickReason, setKickReason] = useState("");
  const [banReason, setBanReason] = useState("");
  const slug = name.replace(/[^A-Za-z0-9_]/g, "_");
  return (
    <li className="flex flex-wrap items-center gap-2 py-2">
      <PlayerAvatar name={name} uuid={uuid} showImage={showHeads} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">{name}</p>
        {isOp && <p className="text-[11px] text-amber-700 dark:text-amber-300">Operator</p>}
      </div>
      {canManage && (
        <div className="flex flex-wrap items-center gap-1">
          {isOp ? (
            <Button size="xs" variant="ghost" disabled={act.isPending} onClick={() => act.mutate({ action: "deop", name })}>
              <ShieldOff /> De-op
            </Button>
          ) : (
            <ConfirmAction
              trigger={
                <Button size="xs" variant="ghost" disabled={act.isPending}>
                  <ShieldCheck /> Op
                </Button>
              }
              title={`Make ${name} an operator?`}
              description="Operators can run server commands in game, including changing game modes, teleporting, banning players and stopping the server."
              confirmLabel="Make operator"
              onConfirm={() => act.mutateAsync({ action: "op", name })}
            />
          )}
          <ConfirmAction
            trigger={
              <Button size="xs" variant="ghost" disabled={act.isPending}>
                <LogOut /> Kick
              </Button>
            }
            title={`Kick ${name}?`}
            description="They are disconnected from the server and can rejoin straight away."
            confirmLabel="Kick"
            destructive
            onConfirm={async () => {
              await act.mutateAsync({ action: "kick", name, reason: kickReason });
              setKickReason("");
            }}
          >
            <ReasonField id={`kick-reason-${slug}`} value={kickReason} onChange={setKickReason} />
          </ConfirmAction>
          <ConfirmAction
            trigger={
              <Button size="xs" variant="ghost" className="text-destructive hover:text-destructive" disabled={act.isPending}>
                <Ban /> Ban
              </Button>
            }
            title={`Ban ${name}?`}
            description="They are disconnected and cannot join again until they are unbanned (Bans tab)."
            confirmLabel="Ban player"
            destructive
            onConfirm={async () => {
              await act.mutateAsync({ action: "ban", name, reason: banReason });
              setBanReason("");
            }}
          >
            <ReasonField id={`ban-reason-${slug}`} value={banReason} onChange={setBanReason} />
          </ConfirmAction>
        </div>
      )}
    </li>
  );
}

export function OnlinePlayers({
  instanceId,
  data,
  canManage,
  showHeads,
}: {
  instanceId: string;
  data: McPlayers;
  canManage: boolean;
  showHeads: boolean;
}) {
  const online = data.online;
  const names = online?.players ?? [];
  // names in the list that are no account name (nicknames, Bedrock players): shown with "use the console" (C-6)
  const others = (online?.other_names ?? []).filter((n) => typeof n === "string" && n);
  const uuidOf = (n: string) => data.known_players.find((k) => sameName(k.name, n))?.uuid;
  const count =
    data.running && online && !online.error && online.online != null
      ? `${online.online}${online.max != null ? ` / ${online.max}` : ""} online`
      : undefined;

  return (
    <Section title="Online now" description={count ?? (data.running ? "Live list from the server" : undefined)}>
      {!data.running ? (
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <WifiOff className="size-4 shrink-0" aria-hidden />
          The server is not running, so nobody is online. Kicks need a running server.
        </div>
      ) : online?.error ? (
        <p className="text-sm text-destructive" role="alert">
          Could not ask the server who is online: {online.error}
        </p>
      ) : online?.online == null && online?.raw ? (
        <div className="grid gap-1 text-sm">
          <p className="text-muted-foreground">The server's reply could not be read as a player list:</p>
          <pre className="overflow-x-auto rounded bg-muted p-2 font-mono text-xs">{online.raw}</pre>
        </div>
      ) : names.length === 0 && others.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nobody is online right now.</p>
      ) : (
        <div className="grid gap-3">
          {names.length > 0 && (
            <ul className="-my-2 divide-y">
              {names.map((n) => (
                <OnlineRow
                  key={n}
                  instanceId={instanceId}
                  name={n}
                  uuid={uuidOf(n)}
                  isOp={data.ops.some((o) => sameName(o.name, n))}
                  canManage={canManage}
                  showHeads={showHeads}
                />
              ))}
            </ul>
          )}
          <ConsoleOnlyNames instanceId={instanceId} names={others} className={names.length > 0 ? "border-t pt-3" : undefined} />
        </div>
      )}
    </Section>
  );
}
