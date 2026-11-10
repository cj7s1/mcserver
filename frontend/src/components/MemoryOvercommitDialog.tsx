/**
 * "Not enough free memory" confirmation for power requests refused with 409 `insufficient_memory`
 * (see lib/power.ts). Mounted once in the app layout; every start path asks through it.
 */
import { MemoryStick } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { setOvercommitAsker, type OvercommitRequest } from "@/lib/power";

interface Pending extends OvercommitRequest {
  key: number;
  resolve: (confirmed: boolean) => void;
}

export function MemoryOvercommitHost() {
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
  return (
    <ConfirmDialog
      key={current.key}
      open
      onOpenChange={(o) => {
        if (!o) settle(false);
      }}
      title={`Not enough free memory to ${verb} ${current.instance.name}`}
      description={
        <div className="grid gap-2">
          <p className="flex items-start gap-2">
            <MemoryStick className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
            <span className="[overflow-wrap:anywhere]">{current.message}</span>
          </p>
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
