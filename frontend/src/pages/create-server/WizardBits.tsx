import { Collapsible } from "radix-ui";
import { Check, ChevronRight } from "lucide-react";
import { type ReactNode, useState } from "react";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { STEPS, type StepId } from "./wizardModel";

/** aria-describedby for an input with an optional error/hint. */
export function describedBy(id: string, error?: string, hint?: ReactNode): string | undefined {
  if (error) return `${id}-error`;
  if (hint) return `${id}-hint`;
  return undefined;
}

export function Field({
  id,
  label,
  hint,
  error,
  optional,
  children,
  className,
}: {
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  error?: string;
  optional?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("grid min-w-0 content-start gap-1.5", className)}>
      <Label htmlFor={id}>
        {label}
        {optional && <span className="font-normal text-muted-foreground">(optional)</span>}
      </Label>
      {children}
      {error ? (
        <p id={`${id}-error`} className="text-xs text-destructive" role="alert">
          {error}
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

/** Progressive disclosure for advanced options. Opens itself when it contains an error. */
export function Advanced({
  title = "Advanced",
  description,
  hasError,
  children,
  className,
}: {
  title?: string;
  description?: ReactNode;
  hasError?: boolean;
  children: ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const isOpen = open || !!hasError;
  return (
    <Collapsible.Root open={isOpen} onOpenChange={setOpen} className={cn("min-w-0 rounded-lg border", className)}>
      <Collapsible.Trigger asChild>
        <button
          type="button"
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-left text-sm font-medium outline-none hover:bg-muted/40 focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          <ChevronRight className={cn("size-4 text-muted-foreground transition-transform", isOpen && "rotate-90")} aria-hidden />
          <span className="shrink-0">{title}</span>
          {description && <span className="ml-1 min-w-0 truncate text-xs font-normal text-muted-foreground">{description}</span>}
          {hasError && <span className="ml-auto shrink-0 text-xs font-normal text-destructive">Needs attention</span>}
        </button>
      </Collapsible.Trigger>
      <Collapsible.Content className="border-t px-3 py-3">{children}</Collapsible.Content>
    </Collapsible.Root>
  );
}

/** Step indicator. Steps up to `maxReached` are clickable. */
export function Stepper({
  steps = STEPS,
  current,
  maxReached,
  onGo,
  labels,
}: {
  steps?: typeof STEPS;
  current: StepId;
  maxReached: number;
  onGo: (step: StepId) => void;
  labels?: Partial<Record<StepId, string>>;
}) {
  const currentIdx = steps.findIndex((s) => s.id === current);
  return (
    <nav aria-label="Create server progress">
      <p className="mb-2 text-xs text-muted-foreground sm:hidden">
        Step {currentIdx + 1} of {steps.length}: <span className="font-medium text-foreground">{labels?.[current] ?? steps[currentIdx].label}</span>
      </p>
      <ol className="flex items-center gap-1 sm:gap-2">
        {steps.map((s, i) => {
          const done = i < currentIdx;
          const active = i === currentIdx;
          const reachable = i <= maxReached && !active;
          return (
            <li key={s.id} className={cn("flex min-w-0 items-center gap-1 sm:gap-2", i < steps.length - 1 && "flex-1")}>
              <button
                type="button"
                onClick={() => reachable && onGo(s.id)}
                disabled={!reachable && !active}
                aria-current={active ? "step" : undefined}
                className={cn(
                  "flex min-w-0 items-center gap-2 rounded-md px-1 py-1 text-left text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
                  reachable && "hover:bg-muted/50",
                  !reachable && !active && "cursor-default",
                )}
              >
                <span
                  className={cn(
                    "grid size-6 shrink-0 place-items-center rounded-full border text-xs font-semibold tabular-nums",
                    active && "border-primary bg-primary text-primary-foreground",
                    done && "border-primary/50 bg-primary/15 text-primary",
                    !active && !done && "text-muted-foreground",
                  )}
                >
                  {done ? <Check className="size-3.5" aria-hidden /> : i + 1}
                </span>
                <span
                  className={cn(
                    "hidden truncate sm:inline",
                    active ? "font-medium text-foreground" : "text-muted-foreground",
                  )}
                >
                  {labels?.[s.id] ?? s.short}
                </span>
                <span className="sr-only">{done ? "(completed)" : active ? "(current)" : ""}</span>
              </button>
              {i < steps.length - 1 && <span className={cn("h-px min-w-2 flex-1", done ? "bg-primary/50" : "bg-border")} aria-hidden />}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/**
 * Card-styled radio option (native radio input, so arrow keys move within the group
 * and screen readers announce it as a radio).
 */
export function ChoiceCard({
  name,
  value,
  checked,
  onChange,
  title,
  description,
  icon,
  meta,
  disabled,
  className,
}: {
  name: string;
  value: string;
  checked: boolean;
  onChange: (value: string) => void;
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  meta?: ReactNode;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <label
      className={cn(
        "relative flex min-w-0 cursor-pointer gap-3 rounded-lg border bg-card p-3 transition-colors has-[:focus-visible]:border-ring has-[:focus-visible]:ring-3 has-[:focus-visible]:ring-ring/50",
        checked ? "border-primary/60 bg-primary/5 ring-1 ring-primary/30" : "hover:bg-muted/40",
        disabled && "cursor-not-allowed opacity-60",
        className,
      )}
    >
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={() => onChange(value)}
        className="sr-only"
      />
      {icon && (
        <span
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-lg [&_svg]:size-4.5",
            checked ? "bg-primary/15 text-primary" : "bg-muted text-muted-foreground",
          )}
          aria-hidden
        >
          {icon}
        </span>
      )}
      <span className="grid min-w-0 flex-1 content-start gap-0.5">
        <span className="text-sm font-medium">{title}</span>
        {description && <span className="text-xs text-muted-foreground">{description}</span>}
        {meta && <span className="mt-1 text-[11px] text-muted-foreground">{meta}</span>}
      </span>
      {checked && <Check className="absolute top-2.5 right-2.5 size-4 text-primary" aria-hidden />}
    </label>
  );
}

/** Compact key/value summary list used by the sidebar and review step. */
export function SummaryList({ rows, className }: { rows: [ReactNode, ReactNode][]; className?: string }) {
  return (
    <dl className={cn("grid grid-cols-[minmax(6rem,auto)_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-sm", className)}>
      {rows.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
