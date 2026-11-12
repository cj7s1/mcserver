/**
 * `<a href download>` for the panel's download routes (D-1 / I-7): a click (also a middle or modified
 * click) gets a ticket for `href` first (lib/downloads.ts) and then lets the browser download the
 * ticketed URL. `href` stays the plain `/api/` URL, so nothing about the link changes for the page.
 * Works as the child of `Button asChild`.
 */
import type { ComponentProps, MouseEvent } from "react";
import { toast } from "sonner";
import { errorMessage } from "@/lib/api";
import { startDownload } from "@/lib/downloads";

export function DownloadLink({ href, download, onClick, onAuxClick, children, ...rest }: ComponentProps<"a"> & { href: string }) {
  const go = (e: MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault();
    startDownload(href, typeof download === "string" && download ? download : undefined).catch((err: unknown) =>
      toast.error(`Download failed: ${errorMessage(err)}`),
    );
  };
  return (
    <a
      {...rest}
      href={href}
      download={download}
      onClick={(e) => {
        onClick?.(e);
        if (!e.defaultPrevented) go(e);
      }}
      onAuxClick={(e) => {
        onAuxClick?.(e);
        if (e.button === 1 && !e.defaultPrevented) go(e);
      }}
    >
      {children}
    </a>
  );
}
