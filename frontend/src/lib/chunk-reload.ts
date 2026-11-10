/**
 * Recovery from stale lazily loaded chunks: after a panel rebuild or upgrade, a tab left open
 * requests chunk file names that no longer exist. Such an error reloads the page once; a second
 * one shortly after shows the error instead, so a broken deployment cannot reload in a loop.
 */

const RELOAD_GUARD_KEY = "mcpanel.chunk-reload-at";
/** A second chunk error within this window after an automatic reload shows the message instead. */
const RELOAD_GUARD_MS = 30_000;

const CHUNK_ERROR =
  /dynamically imported module|Importing a module script failed|Failed to load module script|Unable to preload CSS|error loading dynamically imported module/i;

/** True for the errors a browser raises when a lazily loaded chunk cannot be fetched. */
export function isChunkLoadError(err: unknown): boolean {
  if (!err) return false;
  const message = err instanceof Error ? `${err.name}: ${err.message}` : typeof err === "string" ? err : "";
  return CHUNK_ERROR.test(message);
}

/**
 * Reload the page once for a chunk-load error. Returns false (and does nothing) when a reload was
 * already attempted a moment ago, or when session storage is unavailable, so a broken deployment
 * cannot reload in a loop.
 */
export function reloadOnceForChunkError(now: number = Date.now(), reload: () => void = () => window.location.reload()): boolean {
  try {
    const last = Number(window.sessionStorage.getItem(RELOAD_GUARD_KEY) ?? "0");
    if (Number.isFinite(last) && now - last < RELOAD_GUARD_MS) return false;
    window.sessionStorage.setItem(RELOAD_GUARD_KEY, String(now));
  } catch {
    return false;
  }
  reload();
  return true;
}

/** Install the Vite listener for failed module preloads (call once at start-up). */
export function installChunkErrorReload(): void {
  window.addEventListener("vite:preloadError", (event) => {
    if (reloadOnceForChunkError()) event.preventDefault();
  });
}
