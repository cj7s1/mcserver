import { Box } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/utils";

const TYPE_COLORS: Record<string, string> = {
  VANILLA: "from-emerald-500/30 to-emerald-700/30 text-emerald-700 dark:text-emerald-300",
  PAPER: "from-sky-500/30 to-sky-700/30 text-sky-700 dark:text-sky-300",
  PURPUR: "from-violet-500/30 to-violet-700/30 text-violet-700 dark:text-violet-300",
  FOLIA: "from-teal-500/30 to-teal-700/30 text-teal-700 dark:text-teal-300",
  FABRIC: "from-amber-500/30 to-amber-700/30 text-amber-700 dark:text-amber-200",
  QUILT: "from-fuchsia-500/30 to-fuchsia-700/30 text-fuchsia-700 dark:text-fuchsia-300",
  FORGE: "from-orange-500/30 to-orange-800/30 text-orange-700 dark:text-orange-300",
  NEOFORGE: "from-orange-400/30 to-red-700/30 text-orange-700 dark:text-orange-200",
};

export function ServerIcon({ url, name, type, className }: { url?: string | null; name: string; type?: string; className?: string }) {
  const [failed, setFailed] = useState(false);
  if (url && !failed) {
    return (
      <img
        src={url}
        alt=""
        loading="lazy"
        onError={() => setFailed(true)}
        className={cn("size-10 shrink-0 rounded-lg bg-muted object-cover", className)}
      />
    );
  }
  const initials = name
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <div
      className={cn(
        "flex size-10 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br text-sm font-semibold",
        TYPE_COLORS[type ?? ""] ?? "from-zinc-500/30 to-zinc-700/30 text-zinc-700 dark:text-zinc-300",
        className,
      )}
      aria-hidden
    >
      {initials || <Box className="size-4" />}
    </div>
  );
}
