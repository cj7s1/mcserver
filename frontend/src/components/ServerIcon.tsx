import { Box } from "lucide-react";
import { useState } from "react";
import { variantColor } from "@/lib/games";
import { cn } from "@/lib/utils";

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
        variantColor(type),
        className,
      )}
      aria-hidden
    >
      {initials || <Box className="size-4" />}
    </div>
  );
}
