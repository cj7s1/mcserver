/**
 * Notice for a start or restart refused with 409 `secret_required` (lib/power.ts): the server's game
 * needs a secret that is not set (Valheim's join password). Mounted once in the app layout. It links to
 * Settings -> Game settings, whose Secrets section holds the value; a user who may not configure the
 * server (the section is not shown to them) is told to ask someone who may.
 *
 * `SecretRequiredJobHint` says the same under a failed job whose `error_code` is `secret_required` (a
 * schedule, an update with "start afterwards", a restore or recreate that starts the server).
 */
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { SECRET_REQUIRED, secretsPath, setSecretRequiredNotifier, type SecretRequiredNotice } from "@/lib/power";
import { qk } from "@/lib/queries";
import type { Instance, Job } from "@/lib/types";

/** Whether the signed-in user may configure the server, from the cached server or server list; null when unknown. */
function mayConfigure(qc: QueryClient, instanceId: string): boolean | null {
  const one = qc.getQueryData<Instance>(qk.instance(instanceId));
  const listed =
    one ??
    qc
      .getQueriesData<Instance[]>({ queryKey: qk.instances })
      .flatMap(([, data]) => (Array.isArray(data) ? data : []))
      .find((i) => i?.id === instanceId);
  if (!listed?.permissions) return null;
  return listed.permissions.includes("instance.configure");
}

export function SecretRequiredHost() {
  const qc = useQueryClient();
  const [notice, setNotice] = useState<(SecretRequiredNotice & { seq: number }) | null>(null);

  useEffect(() => {
    let n = 0;
    return setSecretRequiredNotifier((next) => {
      n += 1;
      setNotice({ ...next, seq: n });
    });
  }, []);

  if (!notice) return null;
  const close = () => setNotice(null);
  const what = notice.label ? notice.label.toLowerCase() : "a required secret";
  const verb = notice.action === "restart" ? "restarted" : "started";
  const canSet = mayConfigure(qc, notice.instance.id) !== false;
  return (
    <AlertDialog key={notice.seq} open onOpenChange={(o) => !o && close()}>
      <AlertDialogContent>
        <AlertDialogHeader className="place-items-start text-left">
          <AlertDialogTitle>
            {notice.instance.name} cannot be {verb} without its {what}
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="grid gap-2">
              <p className="flex items-start gap-2">
                <KeyRound className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
                <span className="[overflow-wrap:anywhere]">{notice.message}</span>
              </p>
              <p>
                {canSet
                  ? "Set it under Settings → Game settings → Secrets, then start the server again."
                  : "Ask someone who may configure this server to set it under Settings → Game settings → Secrets."}
              </p>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Close</AlertDialogCancel>
          {canSet && (
            <Button asChild>
              <Link to={secretsPath(notice.instance.id)} onClick={close}>
                Open Secrets
              </Link>
            </Button>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** Under a failed job that could not start the server for a missing secret: where to set it. */
export function SecretRequiredJobHint({ job, className }: { job: Pick<Job, "error_code" | "instance">; className?: string }) {
  if (job.error_code !== SECRET_REQUIRED || !job.instance) return null;
  return (
    <p className={className}>
      <Link to={secretsPath(job.instance)} className="font-medium text-primary hover:underline">
        Set it under Settings → Game settings → Secrets
      </Link>
    </p>
  );
}
