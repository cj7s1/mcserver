/** A restart/stop that is warning players (instance.power_countdown), with a live timer and Cancel. */
import { useMutation } from "@tanstack/react-query";
import { Loader2, Megaphone, X } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { api, errorMessage } from "@/lib/api";
import { countdownText } from "@/lib/power-warning";
import { useInvalidateInstance } from "@/lib/queries";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";

function useNow(intervalMs: number, enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    const t = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(t);
  }, [intervalMs, enabled]);
  return now;
}

export function PowerCountdownBanner({
  instance,
  canControl,
  className,
}: {
  instance: Pick<Instance, "id" | "name" | "power_countdown">;
  canControl: boolean;
  className?: string;
}) {
  const countdown = instance.power_countdown;
  const invalidate = useInvalidateInstance();
  const now = useNow(1000, !!countdown?.action_at);
  const cancel = useMutation({
    mutationFn: () => api.post(`/instances/${instance.id}/power/cancel`),
    onSuccess: () => {
      toast.success(`${countdown?.action === "stop" ? "Stop" : "Restart"} cancelled`);
      invalidate(instance.id);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  if (!countdown) return null;
  const verb = countdown.action === "stop" ? "Stopping" : "Restarting";
  const left = countdownText(countdown.action_at, now);
  const headline =
    countdown.phase === "queued" || !left
      ? `${countdown.action === "stop" ? "Stop" : "Restart"} queued${
          // a friend's short answer (B4) has no warning length
          typeof countdown.warn_minutes === "number" ? ` with a ${countdown.warn_minutes} minute warning` : ""
        }`
      : left === "0:00"
        ? `${verb} now…`
        : `${verb} in ${left}`;

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-900 dark:text-sky-100",
        className,
      )}
      role="status"
      aria-live="polite"
    >
      <Megaphone className="size-4 shrink-0 text-sky-600 dark:text-sky-400" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="font-medium tabular-nums">{headline}</p>
        <p className="text-xs opacity-90 [overflow-wrap:anywhere]">
          {countdown.phase === "queued"
            ? "The countdown starts when a worker picks the job up."
            : "Online players are being warned in chat. Other operations keep working until the last 10 seconds."}
          {countdown.message && <> Message: “{countdown.message}”</>}
        </p>
      </div>
      {canControl && (
        <Button size="sm" variant="outline" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
          {cancel.isPending ? <Loader2 className="animate-spin" /> : <X />} Cancel
        </Button>
      )}
    </div>
  );
}
