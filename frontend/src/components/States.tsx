import { AlertTriangle, Inbox, Loader2, RefreshCw, WifiOff } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";

export function LoadingState({ label = "Loading…", className }: { label?: string; className?: string }) {
  return (
    <div className={cn("flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground", className)} role="status">
      <Loader2 className="size-4 animate-spin" /> {label}
    </div>
  );
}

export function ErrorState({ error, onRetry, title = "Something went wrong", className }: {
  error: unknown;
  onRetry?: () => void;
  title?: string;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center gap-3 rounded-xl border border-destructive/30 bg-destructive/5 px-6 py-10 text-center", className)} role="alert">
      <AlertTriangle className="size-6 text-destructive" />
      <div>
        <p className="font-medium">{title}</p>
        <p className="mt-1 max-w-lg text-sm text-muted-foreground [overflow-wrap:anywhere]">{errorMessage(error)}</p>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RefreshCw /> Retry
        </Button>
      )}
    </div>
  );
}

export function EmptyState({ icon, title, description, action, className }: {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-12 text-center", className)}>
      <div className="rounded-full bg-muted p-3 text-muted-foreground">{icon ?? <Inbox className="size-5" />}</div>
      <div>
        <p className="font-medium">{title}</p>
        {description && <div className="mt-1 max-w-md text-sm text-muted-foreground [overflow-wrap:anywhere]">{description}</div>}
      </div>
      {action}
    </div>
  );
}

/**
 * Non-blocking notice shown above content whose last refresh failed while older data is still on
 * screen (the page stays mounted, so unsaved drafts and open dialogs survive a short outage).
 */
export function ConnectionLostBanner({ error, onRetry, className }: { error?: unknown; onRetry?: () => void; className?: string }) {
  return (
    <div
      className={cn("flex flex-wrap items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm", className)}
      role="status"
    >
      <WifiOff className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />
      <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">
        Connection to the panel lost; retrying. What you see may be out of date.
        {error ? <span className="text-muted-foreground"> ({errorMessage(error)})</span> : null}
      </span>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RefreshCw /> Retry now
        </Button>
      )}
    </div>
  );
}
