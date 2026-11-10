import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useNow } from "../overview/overview-utils";
import type { ConnState } from "./use-console-socket";

const STYLES: Record<ConnState, { label: string; dot: string; text: string; pulse?: boolean; help: string }> = {
  connecting: {
    label: "Connecting…",
    dot: "bg-amber-400",
    text: "text-amber-700 dark:text-amber-300",
    pulse: true,
    help: "Opening the console connection to the panel",
  },
  connected: {
    label: "Live",
    dot: "bg-emerald-400",
    text: "text-emerald-700 dark:text-emerald-300",
    help: "Streaming the server's output in real time",
  },
  agent_lost: {
    label: "Agent connection lost",
    dot: "bg-orange-400",
    text: "text-orange-700 dark:text-orange-300",
    pulse: true,
    help: "The panel lost its log stream from the node agent and is retrying",
  },
  stream_closed: {
    label: "Log stream closed",
    dot: "bg-zinc-400",
    text: "text-muted-foreground",
    help: "The node agent ended the log stream; reconnecting",
  },
  denied: {
    label: "Access ended",
    dot: "bg-zinc-400",
    text: "text-muted-foreground",
    help: "Your session ended or your access to this server's console was revoked; reload the page after signing in again",
  },
  reconnecting: {
    label: "Disconnected",
    dot: "bg-red-500",
    text: "text-destructive",
    help: "The console connection dropped; reconnecting automatically",
  },
};

export function ConnectionIndicator({
  state,
  agentMessage,
  retryAt,
  onRetry,
}: {
  state: ConnState;
  agentMessage: string | null;
  retryAt: number | null;
  onRetry: () => void;
}) {
  const now = useNow(1000);
  const s = STYLES[state];
  const secs = retryAt ? Math.max(0, Math.ceil((retryAt - now) / 1000)) : null;
  const canRetry = state === "reconnecting" || state === "stream_closed" || state === "agent_lost";
  return (
    <div className="flex items-center gap-2" role="status" aria-live="polite">
      <Tooltip>
        <TooltipTrigger asChild>
          <span className={cn("flex items-center gap-1.5 text-xs font-medium", s.text)} tabIndex={0}>
            <span className="relative inline-flex size-2" aria-hidden>
              {s.pulse && <span className={cn("absolute inline-flex size-full animate-ping rounded-full opacity-60", s.dot)} />}
              <span className={cn("relative inline-flex size-2 rounded-full", s.dot)} />
            </span>
            {s.label}
            {secs !== null && (state === "reconnecting" || state === "stream_closed") && (
              <span className="font-normal text-muted-foreground tabular-nums">· retry in {secs}s</span>
            )}
          </span>
        </TooltipTrigger>
        <TooltipContent>{agentMessage ?? s.help}</TooltipContent>
      </Tooltip>
      {canRetry && (
        <Button type="button" variant="ghost" size="xs" onClick={onRetry}>
          <RefreshCw /> Reconnect now
        </Button>
      )}
      {state === "connected" && (
        // A stream can stop delivering lines without the socket closing (e.g. the agent's log pump
        // died during a container recreate): offer a manual re-subscribe in the live state too.
        <Tooltip>
          <TooltipTrigger asChild>
            <Button type="button" variant="ghost" size="icon-xs" onClick={onRetry} aria-label="Reconnect the console">
              <RefreshCw />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Reconnect and reload the log tail (use it if new output stops appearing)</TooltipContent>
        </Tooltip>
      )}
    </div>
  );
}
