import { Fragment, type ReactNode } from "react";
import { cn } from "@/lib/utils";

export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded border border-border bg-muted px-1 font-sans text-[11px] leading-none font-medium text-muted-foreground",
        className,
      )}
    >
      {children}
    </kbd>
  );
}

/** A chord ("Ctrl" + "K") or a sequence ("g" then "d") as key caps, readable by screen readers. */
export function KeyCaps({ keys, sequence = false, className }: { keys: readonly string[]; sequence?: boolean; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      {keys.map((k, i) => (
        <Fragment key={`${k}-${i}`}>
          {i > 0 && (sequence ? <span className="text-[11px] text-muted-foreground">then</span> : <span className="sr-only">+</span>)}
          <Kbd>{k}</Kbd>
        </Fragment>
      ))}
    </span>
  );
}
