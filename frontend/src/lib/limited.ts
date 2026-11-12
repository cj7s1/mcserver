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

/** Whether the signed-in user is only a friend: no global permission beyond the Friend role's, and every server limited. */
export function friendOnly(user: Pick<Me, "permissions" | "is_owner"> | null | undefined, instances: readonly Pick<Instance, "limited">[] | undefined): boolean {
  if (!user || user.is_owner) return false;
  if (user.permissions.some((c) => !FRIEND_CODES.has(c))) return false;
  return !!instances && instances.every((i) => isLimited(i)) && (instances.length > 0 || user.permissions.length === 0);
}

export function useFriendOnly(): boolean {
  const { user } = useAuth();
  const instances = useInstances();
  return friendOnly(user, instances.data);
}
