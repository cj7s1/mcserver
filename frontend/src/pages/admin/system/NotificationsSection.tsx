/** System → Notifications: Discord / webhook channels (GET/POST /notifications/channels, PATCH/DELETE/test per channel). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BellRing, Loader2, Pencil, Plus, Send, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { useInstances } from "@/lib/queries";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ChannelDialog } from "./ChannelDialog";
import {
  channelsKey,
  eventLabels,
  eventsKey,
  KIND_META,
  type NotificationChannel,
  type NotificationEvent,
} from "./notifications-model";

export function NotificationsSection() {
  const events = useQuery({
    queryKey: eventsKey,
    queryFn: () => api.get<{ events: NotificationEvent[] }>("/notifications/events"),
    staleTime: Infinity,
  });
  const channels = useQuery({
    queryKey: channelsKey,
    queryFn: () => api.get<NotificationChannel[]>("/notifications/channels"),
    refetchInterval: 30_000,
  });
  const servers = useInstances("all");
  const [dialog, setDialog] = useState<{ channel?: NotificationChannel } | null>(null);

  const eventList = events.data?.events ?? [];
  const serverList = servers.data ?? [];
  const list = channels.data ?? [];

  return (
    <Section
      title={
        <span className="inline-flex items-center gap-2">
          <BellRing className="size-4 text-muted-foreground" aria-hidden /> Notifications
        </span>
      }
      description="Send server crashes, failed jobs and backup results to Discord or any JSON webhook."
      actions={
        <Button size="sm" onClick={() => setDialog({})} disabled={!events.data}>
          <Plus /> Add channel
        </Button>
      }
    >
      {channels.isLoading ? (
        <LoadingState label="Loading channels…" className="py-8" />
      ) : channels.error && !channels.data ? (
        <ErrorState error={channels.error} title="Couldn't load notification channels" onRetry={() => channels.refetch()} />
      ) : list.length === 0 ? (
        <EmptyState
          icon={<BellRing className="size-5" />}
          title="No notification channels"
          description="Add a Discord webhook or a generic webhook to hear about crashes and failed backups without opening the panel."
          className="py-8"
          action={
            <Button size="sm" variant="outline" onClick={() => setDialog({})} disabled={!events.data}>
              <Plus /> Add channel
            </Button>
          }
        />
      ) : (
        <ul className="grid gap-2" aria-label="Notification channels">
          {list.map((ch) => (
            <ChannelRow
              key={ch.id}
              channel={ch}
              events={eventList}
              servers={serverList}
              onEdit={() => setDialog({ channel: ch })}
              canEdit={!!events.data}
            />
          ))}
        </ul>
      )}
      {events.error && (
        <p className="mt-2 text-xs text-destructive">Couldn't load the event list: {errorMessage(events.error)}</p>
      )}

      <ChannelDialog
        open={dialog !== null}
        onOpenChange={(o) => !o && setDialog(null)}
        channel={dialog?.channel}
        events={eventList}
        servers={serverList}
      />
    </Section>
  );
}

function ChannelRow({
  channel,
  events,
  servers,
  onEdit,
  canEdit,
}: {
  channel: NotificationChannel;
  events: readonly NotificationEvent[];
  servers: readonly Instance[];
  onEdit: () => void;
  canEdit: boolean;
}) {
  const qc = useQueryClient();
  const timers = useRef<number[]>([]);
  useEffect(() => () => timers.current.forEach((t) => window.clearTimeout(t)), []);

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.patch<NotificationChannel>(`/notifications/channels/${channel.id}`, { enabled }),
    onSuccess: (saved) => {
      qc.setQueryData<NotificationChannel[]>(channelsKey, (old) => old?.map((c) => (c.id === saved.id ? saved : c)));
      toast.success(saved.enabled ? `“${saved.name}” enabled` : `“${saved.name}” disabled`);
    },
    onError: (err) => toast.error(errorMessage(err)),
    onSettled: () => qc.invalidateQueries({ queryKey: channelsKey }),
  });

  const test = useMutation({
    mutationFn: () => api.post<{ queued: boolean }>(`/notifications/channels/${channel.id}/test`),
    onSuccess: () => {
      toast.success("Test notification queued", {
        description: "The worker sends it within a few seconds. The result shows as Last sent or Last error on this channel.",
      });
      // the delivery runs in the worker; pick up last_sent_at / last_error when it is done
      for (const ms of [4000, 12_000]) {
        timers.current.push(window.setTimeout(() => qc.invalidateQueries({ queryKey: channelsKey }), ms));
      }
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  const remove = useMutation({
    mutationFn: () => api.delete(`/notifications/channels/${channel.id}`),
    onSuccess: () => {
      toast.success(`Channel “${channel.name}” deleted`);
      qc.invalidateQueries({ queryKey: channelsKey });
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  const server = channel.instance ? servers.find((s) => s.id === channel.instance) : null;
  const labels = eventLabels(channel.events, events);
  const switchId = `nc-row-enabled-${channel.id}`;

  return (
    <li className={cn("grid min-w-0 gap-2 rounded-lg border bg-background/40 p-3", !channel.enabled && "opacity-80")}>
      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <span className="truncate text-sm font-semibold">{channel.name}</span>
            <Badge variant="outline">{KIND_META[channel.kind]?.short ?? channel.kind}</Badge>
            {!channel.enabled && <Badge variant="secondary">Disabled</Badge>}
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            <span className="font-mono">{channel.url_hint || "—"}</span>
            {" · "}
            {channel.instance ? (
              server ? (
                <>
                  only <span className="text-foreground">{server.name}</span>
                </>
              ) : (
                "one server (unknown)"
              )
            ) : (
              "all servers"
            )}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          <div className="mr-1 flex items-center gap-1.5">
            <Switch
              id={switchId}
              checked={channel.enabled}
              disabled={toggle.isPending}
              onCheckedChange={(v) => toggle.mutate(v)}
              aria-label={`${channel.enabled ? "Disable" : "Enable"} channel ${channel.name}`}
            />
            <label htmlFor={switchId} className="text-xs text-muted-foreground">
              {channel.enabled ? "On" : "Off"}
            </label>
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => test.mutate()}
            disabled={!channel.enabled || test.isPending}
            title={channel.enabled ? "Send a test message" : "Enable the channel to send a test"}
            aria-label={`Send a test notification to ${channel.name}`}
          >
            {test.isPending ? <Loader2 className="animate-spin" /> : <Send />} Test
          </Button>
          <Button size="sm" variant="outline" onClick={onEdit} disabled={!canEdit} aria-label={`Edit channel ${channel.name}`}>
            <Pencil /> Edit
          </Button>
          <ConfirmDialog
            trigger={
              <Button size="sm" variant="outline" aria-label={`Delete channel ${channel.name}`}>
                <Trash2 /> <span className="sr-only sm:not-sr-only">Delete</span>
              </Button>
            }
            title={`Delete channel “${channel.name}”?`}
            description="MCPanel stops sending notifications to this destination. The stored webhook URL is deleted."
            confirmLabel="Delete channel"
            destructive
            onConfirm={() => remove.mutateAsync()}
          />
        </div>
      </div>

      <ul className="flex flex-wrap gap-1" aria-label="Events">
        {labels.map((l) => (
          <li key={l}>
            <Badge variant="secondary" className="h-auto max-w-full font-normal whitespace-normal">
              {l}
            </Badge>
          </li>
        ))}
      </ul>

      <p className="text-xs text-muted-foreground">
        Last sent{" "}
        {channel.last_sent_at ? (
          <time dateTime={channel.last_sent_at} title={formatDateTime(channel.last_sent_at)} className="text-foreground">
            {relativeTime(channel.last_sent_at)}
          </time>
        ) : (
          "never"
        )}
      </p>
      {channel.last_error && (
        <p className="rounded-md border border-destructive/30 bg-destructive/5 px-2.5 py-1.5 text-xs break-words text-destructive">
          <span className="font-medium">Last error:</span> {channel.last_error}
        </p>
      )}
    </li>
  );
}
