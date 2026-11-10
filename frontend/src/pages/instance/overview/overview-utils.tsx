/** Small helpers shared by the instance Overview, Console and Activity pages. */
import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { copyText } from "@/lib/clipboard";
import type { InstanceStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

/** A clock that re-renders the caller every `intervalMs` (for uptimes and relative times). */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

export { copyText };

export function CopyButton({ value, label, className }: { value: string; label: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          className={cn("text-muted-foreground", className)}
          aria-label={label}
          onClick={async () => {
            if (await copyText(value)) {
              setCopied(true);
              clearTimeout(timer.current);
              timer.current = setTimeout(() => setCopied(false), 1500);
              toast.success("Copied to clipboard");
            } else {
              toast.error("Could not access the clipboard. Select the text and copy it manually.");
            }
          }}
        >
          {copied ? <Check className="text-emerald-500" /> : <Copy />}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

/** Statuses where a container process exists and is (or should soon be) producing output. */
export const LIVE_STATUSES: InstanceStatus[] = ["running", "unhealthy", "starting", "restarting"];

/** Statuses where RCON is expected to accept commands. */
export const COMMAND_STATUSES: InstanceStatus[] = ["running", "unhealthy"];

export function capitalize(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}
