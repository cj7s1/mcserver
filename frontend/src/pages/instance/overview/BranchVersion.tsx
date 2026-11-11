/**
 * The version of a server that follows a branch instead of a pinned version (catalog `fixed_version`;
 * Satisfactory: Steam's stable branch, the six Steam-era games: Steam's public branch, checked at every
 * start). "Update now" is a restart: the image downloads the newest build of the branch while the server
 * starts. A fixed version number (HKMP 2.4.3, carried by the panel's image) has no "Update now".
 */
import { useMutation } from "@tanstack/react-query";
import { Loader2, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import { updatesOnRestart } from "@/lib/games";
import { sendPower } from "@/lib/power";
import { useInvalidateInstance } from "@/lib/queries";
import type { GameInfo, Instance } from "@/lib/types";

const LIVE = ["running", "unhealthy"];

export function BranchVersion({
  instance,
  info,
  canControl,
}: {
  instance: Instance;
  info: NonNullable<GameInfo["fixed_version"]>;
  canControl: boolean;
}) {
  const invalidate = useInvalidateInstance();
  const restart = useMutation({
    mutationFn: () => sendPower({ id: instance.id, name: instance.name }, { action: "restart" }),
    onSuccess: (outcome) => {
      if (outcome.status === "cancelled") return;
      toast.success("Restarting: the server checks for an update while it starts");
      invalidate(instance.id);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const canUpdate = updatesOnRestart(info) && canControl && LIVE.includes(instance.status) && !instance.active_job && !instance.archived_at;
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
      <span title={info.help}>{info.label}</span>
      {canUpdate && (
        <Button size="xs" variant="outline" onClick={() => restart.mutate()} disabled={restart.isPending} title="Restart: the server downloads the newest build of its branch while it starts">
          {restart.isPending ? <Loader2 className="animate-spin" /> : <RefreshCw />} Update now
        </Button>
      )}
    </span>
  );
}
