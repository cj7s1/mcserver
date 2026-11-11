/** Minecraft player management: online players, whitelist, operators and bans. */
import { FileJson, Radio } from "lucide-react";
import { Link } from "react-router";
import { Section } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/hooks/use-auth";
import { gameLabel, isMinecraft } from "@/lib/games";
import { useInstanceContext } from "./InstanceLayout";
import { GamePlayers } from "./players/GamePlayers";
import { OnlinePlayers } from "./players/OnlinePlayers";
import { PlayerLists } from "./players/PlayerLists";
import { useAvatarPreference, usePlayers } from "./players/player-utils";
import { WhitelistSettings } from "./players/WhitelistSettings";

const RUNNING_LIKE = ["running", "starting", "unhealthy", "restarting"];

function ModeNote({ running }: { running: boolean }) {
  return running ? (
    <div className="flex gap-2.5 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2.5 text-sm">
      <Radio className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-400" aria-hidden />
      <p className="text-muted-foreground">
        <span className="font-medium text-foreground">The server is running.</span> Changes are sent to it through RCON and apply
        immediately; the server saves them to its JSON files itself.
      </p>
    </div>
  ) : (
    <div className="flex gap-2.5 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2.5 text-sm">
      <FileJson className="mt-0.5 size-4 shrink-0 text-sky-600 dark:text-sky-400" aria-hidden />
      <p className="text-muted-foreground">
        <span className="font-medium text-foreground">The server is stopped.</span> Changes are written directly to{" "}
        <span className="font-mono">whitelist.json</span>, <span className="font-mono">ops.json</span> and{" "}
        <span className="font-mono">banned-players.json</span> and take effect the next time it starts.
      </p>
    </div>
  );
}

export default function Players() {
  const { instance, can } = useInstanceContext();
  // other games: their own lists and labels (players/GamePlayers.tsx); Minecraft: as before
  if (!isMinecraft(instance)) return <GamePlayers instance={instance} canManage={can("instance.players") && !instance.archived_at} />;
  return <MinecraftPlayers />;
}

function MinecraftPlayers() {
  const { instance, can } = useInstanceContext();
  const { user } = useAuth();
  const running = RUNNING_LIKE.includes(instance.status);
  const players = usePlayers(instance.id, running);
  const [showHeads, setShowHeads] = useAvatarPreference();
  const canManage = can("instance.players") && !instance.archived_at;

  if (players.isLoading) return <LoadingState label="Loading players…" />;
  if (players.error || !players.data)
    return <ErrorState error={players.error ?? new Error("No data")} title="Could not load players" onRetry={() => players.refetch()} />;
  const data = players.data;

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      <ModeNote running={data.running} />

      <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-3">
        <div className="grid min-w-0 grid-cols-1 gap-4 lg:col-start-3 lg:row-start-1">
          <OnlinePlayers instanceId={instance.id} data={data} canManage={canManage} showHeads={showHeads} />
          <WhitelistSettings
            instance={instance}
            canConfigure={can("instance.configure")}
            running={data.running}
            whitelistCount={data.whitelist.length}
          />
        </div>

        <Section
          className="min-w-0 lg:col-span-2 lg:col-start-1 lg:row-start-1"
          title="Player lists"
          description={`${gameLabel(instance)} accounts allowed, promoted or banned on this server`}
          actions={
            <div className="flex items-center gap-2">
              <Switch id="players-show-heads" size="sm" checked={showHeads} onCheckedChange={setShowHeads} />
              <Label htmlFor="players-show-heads" className="text-xs font-normal text-muted-foreground">
                Player heads
              </Label>
            </div>
          }
        >
          <div className="grid grid-cols-1 gap-4">
            <PlayerLists instanceId={instance.id} data={data} canManage={canManage} showHeads={showHeads} />
            <div className="grid gap-1 border-t pt-3 text-xs text-muted-foreground">
              <p>
                These are {gameLabel(instance)} players, not panel users. Panel logins and their permissions are managed separately
                {can("instance.access") ? (
                  <>
                    {" "}
                    under{" "}
                    <Link to={`/servers/${instance.id}/access`} className="text-primary hover:underline">
                      Access
                    </Link>
                  </>
                ) : null}
                {user?.permissions.includes("system.users") ? (
                  <>
                    {" "}
                    and{" "}
                    <Link to="/admin/users" className="text-primary hover:underline">
                      Users
                    </Link>
                  </>
                ) : null}
                .
              </p>
              <p>
                Player heads are loaded from mc-heads.net, a third-party service that then sees the names and UUIDs shown here. Off by
                default; the choice is remembered in this browser.
              </p>
              {!can("instance.players") && <p>Changing these lists needs the “Manage players” permission.</p>}
            </div>
          </div>
        </Section>
      </div>
    </div>
  );
}
