/**
 * Link downloads with a ticket (D-1 / I-7). A browser that follows a link sends no session token, so the
 * panel accepts a GET of its download routes (files/download, files/archive, saves/export,
 * backups/<id>/download) only with a `ticket` for exactly that URL: `POST /api/auth/download-ticket
 * {"url"}` answers the same URL with `ticket=...`, valid for 60 seconds. The ticket is fetched on the click,
 * and the browser then downloads the answered URL as it would any link (streamed to disk by the browser
 * itself, with its own progress and file name; no blob in memory).
 */
import { api } from "./api";

export interface DownloadTicket {
  url: string;
  expires_in?: number;
}

/** The ticketed URL of an `/api/` download link (as `downloadUrl` builds it, or as the panel sent it). */
export async function ticketUrl(url: string): Promise<string> {
  const answer = await api.post<DownloadTicket>("/auth/download-ticket", { url });
  // only ever navigate to a path of this panel (never to whatever an answer might name)
  if (typeof answer?.url !== "string" || !answer.url.startsWith("/api/")) throw new Error("The panel did not issue a download ticket");
  return answer.url;
}

/** Navigate the browser to `href` like a click on `<a href download>` would. */
export function followDownload(href: string, filename?: string): void {
  const a = document.createElement("a");
  a.href = href;
  // the file name and the attachment answer make the browser save it; `download` keeps the page in place
  a.download = filename ?? "";
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  try {
    a.click();
  } finally {
    a.remove();
  }
}

/** Get a ticket for the download link `url`, then let the browser download it. Rejects when no ticket is issued. */
export async function startDownload(url: string, filename?: string): Promise<void> {
  followDownload(await ticketUrl(url), filename);
}
