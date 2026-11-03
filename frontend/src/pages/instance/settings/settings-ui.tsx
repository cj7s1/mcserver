/** Shared building blocks for the instance Settings tabs. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, Info, Loader2, Lock, Save, Undo2 } from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { api, ApiError } from "@/lib/api";
import { qk, useInvalidateInstance } from "@/lib/queries";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface TabProps {
  instance: Instance;
  can: (code: Instance["permissions"][number]) => boolean;
  onDirtyChange: (dirty: boolean) => void;
}

/** Report a tab's dirty state to the page (for the tab dot and the leave guard). */
export function useReportDirty(dirty: boolean, onDirtyChange: (dirty: boolean) => void) {
  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);
}

/** PATCH /instances/{id} with only the changed fields; keeps the instance cache in sync. */
export function usePatchInstance(instanceId: string) {
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  return useMutation({
    mutationFn: (body: Partial<Instance>) => api.patch<Instance>(`/instances/${instanceId}`, body),
    onSuccess: (data) => {
      qc.setQueryData(qk.instance(instanceId), data);
      invalidate(instanceId);
    },
  });
}

export function fieldErrorsOf(err: unknown): Record<string, string> {
  return err instanceof ApiError ? err.fieldErrors : {};
}

export function FieldError({ id, message }: { id?: string; message?: string | null }) {
  if (!message) return null;
  return (
    <p id={id} className="text-xs text-destructive" role="alert">
      {message}
    </p>
  );
}

export function Field({
  id,
  label,
  hint,
  error,
  children,
  className,
  aside,
}: {
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  children: ReactNode;
  className?: string;
  aside?: ReactNode;
}) {
  return (
    <div className={cn("grid grid-cols-1 content-start gap-1.5", className)}>
      <div className="flex items-baseline justify-between gap-2">
        <Label htmlFor={id}>{label}</Label>
        {aside}
      </div>
      {children}
      {hint && !error && <p className="text-xs text-muted-foreground">{hint}</p>}
      <FieldError message={error} />
    </div>
  );
}

/** Label + description on the left, control (switch) on the right. */
export function InlineField({
  id,
  label,
  hint,
  error,
  children,
  className,
}: {
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("grid grid-cols-1 gap-1", className)}>
      <div className="flex items-start justify-between gap-4">
        <div className="grid grid-cols-1 gap-1">
          <Label htmlFor={id}>{label}</Label>
          {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
        </div>
        <div className="shrink-0 pt-0.5">{children}</div>
      </div>
      <FieldError message={error} />
    </div>
  );
}

/** Progressive disclosure for advanced options. */
export function Advanced({
  title = "Advanced",
  description,
  children,
  defaultOpen = false,
}: {
  title?: string;
  description?: string;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  return (
    <div className="rounded-lg border">
      <button
        type="button"
        className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm font-medium hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
      >
        <ChevronDown className={cn("size-4 text-muted-foreground transition-transform", open && "rotate-180")} aria-hidden />
        {title}
        {description && <span className="truncate text-xs font-normal text-muted-foreground">{description}</span>}
      </button>
      {open && (
        <div id={id} className="grid grid-cols-1 gap-4 border-t p-3">
          {children}
        </div>
      )}
    </div>
  );
}

/** Sticky footer shown while a form has unsaved changes. */
export function SaveBar({
  count,
  saving,
  disabled,
  onSave,
  onDiscard,
  note,
}: {
  count: number;
  saving: boolean;
  disabled?: boolean;
  onSave: () => void;
  onDiscard: () => void;
  note?: ReactNode;
}) {
  if (count === 0) return null;
  return (
    <div
      className="sticky bottom-3 z-20 flex flex-wrap items-center gap-2 rounded-xl border bg-card/95 px-4 py-2.5 shadow-lg backdrop-blur supports-backdrop-filter:bg-card/80"
      role="region"
      aria-label="Unsaved changes"
    >
      <span className="text-sm font-medium">
        {count} unsaved change{count === 1 ? "" : "s"}
      </span>
      {note && <span className="text-xs text-muted-foreground">{note}</span>}
      <div className="ml-auto flex gap-2">
        <Button variant="outline" size="sm" onClick={onDiscard} disabled={saving}>
          <Undo2 /> Discard
        </Button>
        <Button size="sm" onClick={onSave} disabled={saving || disabled}>
          {saving ? <Loader2 className="animate-spin" /> : <Save />} Save changes
        </Button>
      </div>
    </div>
  );
}

export function Notice({
  tone = "info",
  title,
  children,
  action,
  className,
}: {
  tone?: "info" | "warning" | "danger";
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  const styles = {
    info: "border-sky-500/30 bg-sky-500/10 text-sky-900 dark:text-sky-100 [&_svg]:text-sky-600 dark:[&_svg]:text-sky-400",
    warning: "border-amber-500/30 bg-amber-500/10 text-amber-900 dark:text-amber-100 [&_svg]:text-amber-600 dark:[&_svg]:text-amber-400",
    danger: "border-destructive/30 bg-destructive/10 text-destructive [&_svg]:text-destructive",
  }[tone];
  const Icon = tone === "info" ? Info : AlertTriangle;
  return (
    <div className={cn("flex flex-wrap items-start gap-x-3 gap-y-2 rounded-lg border px-3 py-2.5 text-sm", styles, className)} role={tone === "info" ? "note" : "alert"}>
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1">
        {title && <p className="font-medium">{title}</p>}
        {children && <div className={cn("text-xs opacity-90", title && "mt-0.5")}>{children}</div>}
      </div>
      {action && <div className="flex shrink-0 items-center gap-2">{action}</div>}
    </div>
  );
}

export function ReadOnlyNotice({ what = "change these settings" }: { what?: string }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
      <Lock className="size-3.5 shrink-0" aria-hidden />
      Read-only: you need the “Configure” permission on this server to {what}.
    </div>
  );
}

/** Parse a number input; empty string -> null. */
export function parseNum(v: string): number | null {
  if (v.trim() === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
