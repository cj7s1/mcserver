/**
 * Players page of games that report a player count only (capability "players" without
 * "player_actions"; Satisfactory: its API has no player names, lists or kicks). The count comes from
 * GET /instances/{id}/players (`count_only`, `online`: {online, max}).
 */
import { Users } from "lucide-react";
import { Section } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { gameLabel } from "@/lib/games";
import type { Instance, PlayersResponse } from "@/lib/types";
import { usePlayers } from "./player-utils";

const LIVE = ["running", "starting", "unhealthy", "restarting"];

export function PlayerCount({ instance }: { instance: Instance }) {
  const running = LIVE.includes(instance.status);
  const query = usePlayers(instance.id, running);
  if (query.isLoading) return <LoadingState label="Loading players…" />;
  if (query.error || !query.data) return <ErrorState error={query.error ?? new Error("No data")} title="Could not load players" onRetry={() => query.refetch()} />;
  const data = query.data as unknown as PlayersResponse;
  const online = data.online;
  const count = online?.online ?? null;
  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      <Section title="Online" description={`${gameLabel(instance)} reports how many players are connected, not who: there are no names, lists or kicks here.`}>
        {!data.running ? (
          <p className="text-sm text-muted-foreground">The server is not running.</p>
        ) : online?.error ? (
          <p className="text-sm text-destructive">{online.error}</p>
        ) : (
          <p className="flex items-center gap-2 text-2xl font-semibold tabular-nums" aria-label="Players online">
            <Users className="size-5 text-muted-foreground" aria-hidden />
            {count ?? "—"}
            {online?.max ? <span className="text-sm font-normal text-muted-foreground">of {online.max}</span> : null}
          </p>
        )}
        {data.running && !online?.error && count === null && (
          <p className="mt-2 text-xs text-muted-foreground">The count appears once the server has finished its setup (claim) and loaded its game.</p>
        )}
      </Section>
    </div>
  );
}
