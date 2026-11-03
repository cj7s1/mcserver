import { useState } from "react";
import { cn } from "@/lib/utils";
import { avatarUrl } from "./player-utils";

const HUES = [
  "bg-sky-500/15 text-sky-700 dark:text-sky-300",
  "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  "bg-violet-500/15 text-violet-700 dark:text-violet-300",
  "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  "bg-rose-500/15 text-rose-700 dark:text-rose-300",
  "bg-teal-500/15 text-teal-700 dark:text-teal-300",
];

function hue(name: string): string {
  let h = 0;
  for (const ch of name.toLowerCase()) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return HUES[h % HUES.length];
}

/** Player head from mc-heads.net when enabled, otherwise (or on failure) initials. */
export function PlayerAvatar({
  name,
  uuid,
  showImage,
  className,
}: {
  name: string;
  uuid?: string;
  showImage: boolean;
  className?: string;
}) {
  const url = showImage ? avatarUrl({ uuid, name }) : null;
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  if (url && failedUrl !== url) {
    return (
      <img
        src={url}
        alt=""
        width={32}
        height={32}
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={() => setFailedUrl(url)}
        className={cn("size-7 shrink-0 rounded-md bg-muted [image-rendering:pixelated]", className)}
      />
    );
  }
  const initials = (name.replace(/[^A-Za-z0-9]/g, "").slice(0, 2) || "?").toUpperCase();
  return (
    <span
      className={cn("inline-flex size-7 shrink-0 items-center justify-center rounded-md text-[11px] font-semibold", hue(name), className)}
      aria-hidden
    >
      {initials}
    </span>
  );
}
