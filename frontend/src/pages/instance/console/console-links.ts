/**
 * Links in console output (D-1 part 3 of the security review 2026-11-11). Whatever runs in the game
 * server, and any player whose chat reaches the log, can print a link; a click must never open it
 * without the user seeing where it goes. A link to the panel's own host name on another port is the
 * dangerous kind: browsers send a host's cookies to every port of it (RFC 6265), so the panel's session
 * cookie would reach whatever answers there, e.g. the game server's own published port.
 */

export type ConsoleLinkKind =
  /** the panel's host name, another port (or scheme): the browser would send the panel's cookies there */
  | "panel-other-port"
  /** the panel itself (same scheme, host and port) */
  | "same-origin"
  /** any other host */
  | "external";

export interface ConsoleLink {
  /** The URL that is opened (parsed and normalised: what the browser will request). */
  url: string;
  kind: ConsoleLinkKind;
  /** Host name and effective port of the link (the scheme's default when the URL has none). */
  host: string;
  port: string;
}

const DEFAULT_PORTS: Record<string, string> = { "http:": "80", "https:": "443" };

const hostKey = (host: string) => host.toLowerCase().replace(/\.$/, "");

/**
 * What a link from console output is, relative to the page's own location; null for anything that is no
 * http(s) URL (it is not opened at all).
 */
export function classifyConsoleLink(uri: string, here: Pick<Location, "protocol" | "hostname" | "port">): ConsoleLink | null {
  let url: URL;
  try {
    url = new URL(uri);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  const port = url.port || DEFAULT_PORTS[url.protocol];
  const herePort = here.port || DEFAULT_PORTS[here.protocol] || "";
  let kind: ConsoleLinkKind = "external";
  if (hostKey(url.hostname) === hostKey(here.hostname)) {
    kind = port === herePort && url.protocol === here.protocol ? "same-origin" : "panel-other-port";
  }
  return { url: url.href, kind, host: url.hostname, port };
}
