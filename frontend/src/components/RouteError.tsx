/**
 * Error boundaries for the route tree.
 *
 * Every page is a lazily loaded chunk with a hashed file name. After the panel is rebuilt or
 * upgraded, a tab that was left open asks for chunk names that no longer exist; the import rejects
 * (and React.lazy caches the rejection, so navigating does not help). Such a "chunk load" error
 * reloads the page once (guarded against loops); anything else shows a message inside the layout
 * with a Reload button instead of replacing the whole app with react-router's default screen.
 */
import { AlertTriangle, RefreshCw } from "lucide-react";
import { Component, useEffect, type ErrorInfo, type ReactNode } from "react";
import { useRouteError } from "react-router";
import { Button } from "@/components/ui/button";
import { isChunkLoadError, reloadOnceForChunkError } from "@/lib/chunk-reload";

export function PageError({ error, className }: { error: unknown; className?: string }) {
  const chunk = isChunkLoadError(error);
  return (
    <div className={className ?? "p-6"}>
      <div className="flex flex-col items-center gap-3 rounded-xl border border-destructive/30 bg-destructive/5 px-6 py-10 text-center" role="alert">
        <AlertTriangle className="size-6 text-destructive" />
        <div>
          <p className="font-medium">{chunk ? "The panel was updated" : "This page could not be shown"}</p>
          <p className="mt-1 max-w-lg text-sm text-muted-foreground [overflow-wrap:anywhere]">
            {chunk
              ? "This tab is still running the previous version. Reload to load the new one."
              : error instanceof Error && error.message
                ? error.message
                : "An unexpected error occurred while rendering this page."}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
          <RefreshCw /> Reload
        </Button>
      </div>
    </div>
  );
}

type BoundaryState = { error: unknown };

/** Catches render and lazy-load errors of one page so the sidebar and header stay usable. */
export class PageErrorBoundary extends Component<{ children: ReactNode }, BoundaryState> {
  state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): BoundaryState {
    return { error };
  }

  componentDidCatch(error: unknown, _info: ErrorInfo) {
    if (isChunkLoadError(error)) reloadOnceForChunkError();
  }

  render() {
    if (this.state.error) return <PageError error={this.state.error} />;
    return this.props.children;
  }
}

/** `errorElement` of the root routes: the last line of defence when a layout itself fails. */
export function RouteErrorScreen() {
  const error = useRouteError();
  useEffect(() => {
    if (isChunkLoadError(error)) reloadOnceForChunkError();
  }, [error]);
  return <PageError error={error} className="mx-auto max-w-2xl p-6" />;
}
