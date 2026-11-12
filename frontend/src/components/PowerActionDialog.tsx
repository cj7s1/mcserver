/**
 * Restart/Stop confirmation with the optional in-game warning
 * (POST /instances/{id}/power {action, warn_minutes, warn_message}; backend power_warnings.py).
 */
import { useId, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ApiError, errorMessage } from "@/lib/api";
import { sendPower } from "@/lib/power";
import {
  describeWarning,
  isWarnMinutes,
  powerBody,
  powerToast,
  WARN_MESSAGE_MAX,
  WARN_MINUTES,
  warnMessageError,
  warnOptionLabel,
  type WarnableAction,
  type WarnMinutes,
} from "@/lib/power-warning";
import { useInvalidateInstance, useStats } from "@/lib/queries";
import type { Instance, Job } from "@/lib/types";
import { useStoredState } from "@/pages/library/server-utils";

const COPY: Record<WarnableAction, { title: string; body: string; confirm: string; verb: string }> = {
  restart: {
    title: "Restart",
    body: "The server saves the world, stops and starts again. Connected players are disconnected.",
    confirm: "Restart",
    verb: "restart",
  },
  stop: {
    title: "Stop",
    body: "The server saves the world and stops. Connected players are disconnected.",
    confirm: "Stop server",
    verb: "stop",
  },
};

function PlayersHint({ instanceId, minutes, verb }: { instanceId: string; minutes: number; verb: string }) {
  const stats = useStats(instanceId);
  const online = stats.data?.players?.online;
  if (minutes === 0 || online === undefined || online === null) return null;
  return (
    <p className="text-xs text-muted-foreground" aria-live="polite">
      {online === 0
        ? `Nobody is online right now, so the ${verb} happens at once.`
        : `${online} player${online === 1 ? " is" : "s are"} online and will see the countdown in chat.`}
    </p>
  );
}

export function PowerActionDialog({
  instance,
  action,
  open,
  onOpenChange,
  description,
  onCloseAutoFocus,
}: {
  /** `limited` (a friend, B4): a plain start/stop, without the in-game warning the panel refuses them. */
  instance: Pick<Instance, "id" | "name"> & { limited?: boolean };
  action: WarnableAction;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Replaces the default explanation (e.g. the command palette's stop timeout and restart notes). */
  description?: ReactNode;
  onCloseAutoFocus?: (event: Event) => void;
}) {
  const uid = useId();
  const invalidate = useInvalidateInstance();
  const [storedMinutes, setMinutes] = useStoredState<WarnMinutes>("mcpanel.power.warnMinutes", 0, isWarnMinutes);
  const [message, setMessage] = useState("");
  const [serverError, setServerError] = useState<string | null>(null);
  const copy = COPY[action];
  const plain = !!instance.limited;
  const minutes = plain ? 0 : storedMinutes;
  const localError = minutes ? warnMessageError(message) : null;
  const messageError = localError ?? serverError;

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={(o) => {
        if (!o) setServerError(null);
        onOpenChange(o);
      }}
      title={`${copy.title} ${instance.name}?`}
      confirmLabel={minutes ? `${copy.confirm} in ${minutes} min` : copy.confirm}
      description={<p>{description ?? copy.body}</p>}
      onCloseAutoFocus={onCloseAutoFocus}
      onConfirm={async () => {
        if (localError) throw new Error(localError);
        try {
          const outcome = await sendPower<Job>({ id: instance.id, name: instance.name }, powerBody(action, minutes, message));
          if (outcome.status === "cancelled") return; // not enough memory and the user declined
          toast.success(powerToast(instance.name, copy.title, minutes, outcome.data));
          setMessage("");
          invalidate(instance.id);
        } catch (err) {
          if (err instanceof ApiError && err.fieldErrors.warn_message) setServerError(err.fieldErrors.warn_message);
          else toast.error(errorMessage(err));
          throw err;
        }
      }}
    >
      {!plain && (
        <div className="grid gap-3 rounded-lg border bg-muted/30 p-3">
          <div className="grid gap-1.5">
            <Label htmlFor={`${uid}-warn`}>Warn players in game</Label>
            <Select value={String(minutes)} onValueChange={(v) => setMinutes(Number(v) as WarnMinutes)}>
              <SelectTrigger id={`${uid}-warn`} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {WARN_MINUTES.map((m) => (
                  <SelectItem key={m} value={String(m)}>
                    {warnOptionLabel(m)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              {minutes
                ? `${describeWarning(minutes)} Other operations keep working meanwhile; you can cancel until the last 10 seconds.`
                : `The ${copy.verb} starts immediately.`}
            </p>
            <PlayersHint instanceId={instance.id} minutes={minutes} verb={copy.verb} />
          </div>
          {minutes > 0 && (
            <div className="grid gap-1.5">
              <Label htmlFor={`${uid}-msg`}>Message (optional)</Label>
              <Input
                id={`${uid}-msg`}
                value={message}
                maxLength={WARN_MESSAGE_MAX}
                placeholder="e.g. Installing the new modpack version"
                onChange={(e) => {
                  setMessage(e.target.value.replace(/[\r\n]/g, " "));
                  setServerError(null);
                }}
                aria-invalid={!!messageError || undefined}
                aria-describedby={`${uid}-msg-help`}
                autoComplete="off"
              />
              <p id={`${uid}-msg-help`} className={messageError ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
                {messageError ?? "Shown after each warning. Plain text, one line."}
              </p>
            </div>
          )}
        </div>
      )}
    </ConfirmDialog>
  );
}
