/**
 * "Not enough free memory" confirmation for power requests refused with 409 `insufficient_memory`
 * (see lib/power.ts). Mounted once in the app layout; every start path asks through it.
 *
 * Starting anyway (`allow_overcommit`) is an administrator's decision: the panel honours it only for a
 * user whose global permissions include `system.settings` (anyone else gets 403 `overcommit_not_allowed`,
 * D-4). So "Start anyway" is offered only to such users; the others see the same figures with "ask an
 * administrator" and a Close button. While the signed-in user is not known (no auth state cached, e.g. a
 * test without one) the choice is offered as before and the panel decides.
 */
import { useQueryClient } from "@tanstack/react-query";
import { MemoryStick } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { mayOvercommit, setOvercommitAsker, type OvercommitRequest } from "@/lib/power";
import { qk } from "@/lib/queries";
import type { AuthState } from "@/lib/types";

interface Pending extends OvercommitRequest {
  key: number;
  resolve: (confirmed: boolean) => void;
}

export function MemoryOvercommitHost() {
  const qc = useQueryClient();
  const [queue, setQueue] = useState<Pending[]>([]);
  const nextKey = useRef(0);

  useEffect(
    () =>
      setOvercommitAsker(
        (req) =>
          new Promise<boolean>((resolve) => {
            nextKey.current += 1;
            setQueue((q) => [...q, { ...req, key: nextKey.current, resolve }]);
          }),
      ),
    [],
  );

  // Unmounted with requests still open (e.g. signed out): they count as declined.
  const queueRef = useRef(queue);
  useEffect(() => {
    queueRef.current = queue;
  }, [queue]);
  useEffect(() => () => queueRef.current.forEach((p) => p.resolve(false)), []);

  const current = queue[0];
  if (!current) return null;
  const settle = (confirmed: boolean) => {
    current.resolve(confirmed); // a promise settles once: the close after "Start anyway" is a no-op
    setQueue((q) => q.filter((p) => p.key !== current.key));
  };
  const verb = current.action === "restart" ? "restart" : "start";
  const title = `Not enough free memory to ${verb} ${current.instance.name}`;
  const figures = (
    <p className="flex items-start gap-2">
      <MemoryStick className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
      <span className="[overflow-wrap:anywhere]">{current.message}</span>
    </p>
  );

  if (mayOvercommit(qc.getQueryData<AuthState>(qk.auth)) === false) {
    return (
      <AlertDialog
        key={current.key}
        open
        onOpenChange={(o) => {
          if (!o) settle(false);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader className="place-items-start text-left">
            <AlertDialogTitle>{title}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="grid gap-2">
                {figures}
                <p>
                  Starting a server that does not fit into the host's memory needs an administrator (the permission to change
                  system settings). Ask an administrator, or stop another server first.
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Close</AlertDialogCancel>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    );
  }

  return (
    <ConfirmDialog
      key={current.key}
      open
      onOpenChange={(o) => {
        if (!o) settle(false);
      }}
      title={title}
      description={
        <div className="grid gap-2">
          {figures}
          <p>
            Every server reserves its whole memory limit while it runs. Starting anyway can make the host run out of
            memory, and the system may then kill this or another server (or another container) without saving.
          </p>
        </div>
      }
      confirmLabel="Start anyway"
      destructive
      onConfirm={() => settle(true)}
    />
  );
}
