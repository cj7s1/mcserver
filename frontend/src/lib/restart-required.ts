/**
 * Wording of "restart required" (Instance.restart_required). The container is out of date for one
 * of two reasons: a setting of the server changed, or the container was made by an older MCPanel
 * version (after an upgrade every server that has a container reads restart-required although
 * none of its settings changed). The texts have to be true for both. A server without a container
 * (stopped: its container was removed) never reads restart-required: its next start creates the
 * container from the current settings.
 */
import type { Instance } from "./types";

/** Short form for server cards. */
export const RESTART_REQUIRED_NOTE = "Restart required: changed settings or a newer MCPanel version";

/** The banner on a server's own pages. */
export function restartRequiredText(instance: Pick<Instance, "status">): string {
  return instance.status === "stopped" || instance.status === "failed"
    ? "The container is recreated the next time the server starts, to apply changed settings or a newer MCPanel version. World data is kept."
    : "Restart required: the container is recreated to apply changed settings or a newer MCPanel version. World data is kept.";
}
