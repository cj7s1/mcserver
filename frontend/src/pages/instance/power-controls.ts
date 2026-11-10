/**
 * Which power buttons a server's header shows (InstanceLayout).
 *
 * A stopped server has no container: the panel removes it once the server is meant to be
 * stopped (`desired_state`) and keeps the end of its console output. A server that is meant to
 * run but is not running keeps its container, so that what happened can be looked at: it
 * failed, it stopped by itself, or it waits to be started again after a host shutdown. `dismiss`
 * is the Stop button for exactly that case. It settles the server as stopped, and its container
 * is then removed like that of any stopped server. `container_state` is empty when there is no
 * container.
 */
import type { Instance } from "@/lib/types";

const RUNNING_LIKE: Instance["status"][] = ["running", "starting", "installing", "unhealthy", "restarting"];

export interface PowerButtons {
  start: boolean;
  restart: boolean;
  stop: boolean;
  /** Stop for a server that is not running but still meant to run (its container is kept until someone decides). */
  dismiss: boolean;
  kill: boolean;
}

export function powerButtons(instance: Pick<Instance, "status" | "container_state" | "desired_state">): PowerButtons {
  const running = RUNNING_LIKE.includes(instance.status);
  return {
    start: !running,
    restart: running,
    stop: running,
    dismiss:
      (instance.status === "failed" || instance.status === "stopped") &&
      !!instance.container_state &&
      instance.desired_state === "running",
    kill: running || instance.status === "stopping",
  };
}

export const DISMISS_HINT =
  "Mark the server as stopped. Its container is removed; the end of its console output stays available.";
