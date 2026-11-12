/**
 * A list the node cut off at its cap (B-6: a folder listing or the backup browser at 20,000 entries, a file
 * search after 50,000 files or 30 s): said plainly, so the list is never taken for the whole content.
 */
import { ListX } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function CutOffNotice({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p
      role="note"
      aria-label="List cut off"
      className={cn(
        "flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-900 dark:text-amber-100",
        className,
      )}
    >
      <ListX className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      <span>{children}</span>
    </p>
  );
}
