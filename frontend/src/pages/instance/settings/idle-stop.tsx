/**
 * Stop when empty (B2): `idle_stop_minutes` of the instance (0 = off, else 5..1440), saved on its own (no restart
 * needed). A server whose player count the panel cannot read (`idle_stop_unavailable`: Valheim) shows the reason
 * instead of the field. Answers of an older panel have neither key: nothing is shown.
 */
import { Loader2, Save, Timer } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/api";
import type { Instance } from "@/lib/types";
import { Field, fieldErrorsOf, Notice, parseNum, usePatchInstance } from "./settings-ui";

export const IDLE_STOP_MIN = 5;
export const IDLE_STOP_MAX = 1440;

/** Why a value cannot be saved, or null. */
export function idleStopError(raw: string): string | null {
  const n = parseNum(raw.trim());
  if (n === null || !Number.isInteger(n)) return "A whole number of minutes";
  if (n !== 0 && (n < IDLE_STOP_MIN || n > IDLE_STOP_MAX)) return `0 (off) or ${IDLE_STOP_MIN} to ${IDLE_STOP_MAX} minutes`;
  return null;
}

/** The plain sentence of what the setting does. */
export function idleStopText(minutes: number): string {
  return minutes > 0
    ? `Stops the server after ${minutes} minute${minutes === 1 ? "" : "s"} with nobody on it; it saves the world first. It stays stopped until someone starts it again.`
    : "Off: the server keeps running when nobody is on it.";
}

export function IdleStopSection({ instance, canEdit }: { instance: Instance; canEdit: boolean }) {
  const patch = usePatchInstance(instance.id);
  const saved = instance.idle_stop_minutes ?? 0;
  const [text, setText] = useState<string | null>(null);
  if (instance.idle_stop_minutes === undefined && instance.idle_stop_unavailable === undefined) return null;
  const value = text ?? String(saved);
  const error = idleStopError(value);
  const serverError = fieldErrorsOf(patch.error).idle_stop_minutes;
  const minutes = error ? null : Number(value.trim());
  const dirty = minutes !== null && minutes !== saved;
  const save = () => {
    if (minutes === null) return;
    patch.mutate(
      { idle_stop_minutes: minutes },
      {
        onSuccess: () => {
          setText(null);
          toast.success(minutes ? `Stops when empty for ${minutes} minutes` : "Stop when empty turned off");
        },
        onError: (err) => {
          if (!fieldErrorsOf(err).idle_stop_minutes) toast.error(errorMessage(err));
        },
      },
    );
  };
  return (
    <Section
      title={
        <span className="flex items-center gap-2">
          <Timer className="size-4 text-muted-foreground" aria-hidden /> Stop when empty
        </span>
      }
      description="Saves memory and power when nobody plays. Applies at once, no restart needed."
    >
      {instance.idle_stop_unavailable ? (
        <Notice tone="info">{instance.idle_stop_unavailable}</Notice>
      ) : (
        <div className="grid gap-3">
          <Field
            id="idle-stop-minutes"
            label="Minutes without players (0 = off)"
            error={(text !== null ? error : null) ?? serverError ?? null}
            hint={idleStopText(minutes ?? saved)}
          >
            <div className="flex flex-wrap items-center gap-2">
              <Input
                id="idle-stop-minutes"
                type="number"
                inputMode="numeric"
                min={0}
                max={IDLE_STOP_MAX}
                className="w-28 tabular-nums"
                value={value}
                onChange={(e) => {
                  setText(e.target.value);
                  if (patch.error) patch.reset();
                }}
                disabled={!canEdit || patch.isPending}
                aria-invalid={!!((text !== null && error) || serverError) || undefined}
              />
              {canEdit && (
                <Button size="sm" onClick={save} disabled={!dirty || patch.isPending}>
                  {patch.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save
                </Button>
              )}
            </div>
          </Field>
          <p className="text-xs text-muted-foreground">
            The panel checks the player count once a minute. It never stops the server while a backup, update or other job runs, or
            while it cannot read the count.
          </p>
        </div>
      )}
    </Section>
  );
}
