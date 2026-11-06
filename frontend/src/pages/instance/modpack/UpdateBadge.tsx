/** "Update available: <release>" from the periodic modpack update check; opens the update plan on the Modpack tab. */
import { ArrowUpCircle } from "lucide-react";
import { Link } from "react-router";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";
import { availableUpdate, reviewUpdatePath } from "./modpack-api";

export function ModpackUpdateBadge({ instance, className }: { instance: Instance; className?: string }) {
  const latest = availableUpdate(instance);
  if (!latest) return null;
  return (
    <Link
      to={reviewUpdatePath(instance.id, latest.id)}
      className={cn(
        "inline-flex h-6 max-w-full min-w-0 items-center gap-1 rounded-md bg-emerald-500/10 px-2 text-xs font-medium text-emerald-700 transition-colors hover:bg-emerald-500/20 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none dark:text-emerald-300",
        className,
      )}
      title={`A newer release for Minecraft ${latest.minecraft_version || "this version"} is available. Review what changes before updating.`}
    >
      <ArrowUpCircle className="size-3.5 shrink-0" aria-hidden />
      <span className="truncate">Update available: {latest.name}</span>
    </Link>
  );
}
