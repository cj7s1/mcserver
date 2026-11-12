/**
 * Friends in the panel (B4). A user with the Friend role on a server gets that server's short answer
 * (`limited: true`: status keys only) and may only see it, start/stop it and read its Share card. The app then
 * offers nothing that would answer 403: no server tabs or settings for such a server, and for a user who is
 * only a friend (no other global permission, every listed server limited) none of the pages around servers
 * (modpacks, activity) either.
 */
import { useAuth } from "@/hooks/use-auth";
import { useInstances } from "./queries";
import type { Instance, Me } from "./types";

/** The codes of the Friend role: a user whose global permissions hold nothing else is no more than a friend. */
const FRIEND_CODES = new Set<string>(["instance.control", "instance.overview"]);

export function isLimited(instance: Pick<Instance, "limited"> | null | undefined): boolean {
  return instance?.limited === true;
}

/** The user holds a global permission beyond the Friend role's (or is the owner). */
function beyondFriend(user: Pick<Me, "permissions" | "is_owner">): boolean {
  return user.is_owner || user.permissions.some((c) => !FRIEND_CODES.has(c));
}

/**
 * Whether the signed-in user is only a friend: no global permission beyond the Friend role's, and no server
 * it may view (every listed server limited, or none). The panel answers such an account 403 on its global
 * answers (system info, ports, nodes, roles, permissions, catalogs, notification events).
 */
export function friendOnly(user: Pick<Me, "permissions" | "is_owner"> | null | undefined, instances: readonly Pick<Instance, "limited">[] | undefined): boolean {
  if (!user || beyondFriend(user)) return false;
  return !!instances && instances.every((i) => isLimited(i));
}

/**
 * Whether the panel's global answers (system info, permissions, catalogs, ...) are open to the user: a global
 * permission beyond the Friend role's, or a server it may view. False while the server list is still loading
 * for a user without such a permission, so nothing is asked that could answer 403.
 */
export function globalAnswersAllowed(user: Pick<Me, "permissions" | "is_owner"> | null | undefined, instances: readonly Pick<Instance, "limited">[] | undefined): boolean {
  if (!user) return false;
  if (beyondFriend(user)) return true;
  return !!instances && instances.some((i) => !isLimited(i));
}

export function useFriendOnly(): boolean {
  const { user } = useAuth();
  const instances = useInstances();
  return friendOnly(user, instances.data);
}

export function useGlobalAnswersAllowed(): boolean {
  const { user } = useAuth();
  const instances = useInstances();
  return globalAnswersAllowed(user, instances.data);
}

const JOB_KIND_LABELS: Record<string, string> = { start: "Starting", stop: "Stopping", restart: "Restarting", install: "Installing" };

/** The title of a running job; a friend's short answer has only its kind ("start" -> "Starting"). */
export function activeJobLabel(job: { title?: string; kind: string }): string {
  if (job.title) return job.title;
  return JOB_KIND_LABELS[job.kind] ?? job.kind.charAt(0).toUpperCase() + job.kind.slice(1).replace(/_/g, " ");
}

/** "3 of 10 players online" from a friend's short answer, or null while the count is unknown (or not in the answer). */
export function playersOnlineText(i: Pick<Instance, "players_online" | "players_max">): string | null {
  const n = i.players_online;
  if (typeof n !== "number") return null;
  const max = typeof i.players_max === "number" && i.players_max > 0 ? ` of ${i.players_max}` : "";
  return `${n}${max} player${n === 1 && !max ? "" : "s"} online`;
}
