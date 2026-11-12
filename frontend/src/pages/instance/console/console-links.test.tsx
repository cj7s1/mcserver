/**
 * D-1 part 3 (security review 2026-11-11): a link in console output is opened only after the user saw the
 * whole URL; a link to the panel's own host name on another port (where the browser would send the
 * panel's cookies) gets a strong warning.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { classifyConsoleLink, type ConsoleLink } from "./console-links";
import { ConsoleLinkDialog } from "./ConsoleLinkDialog";

const PANEL = { protocol: "http:", hostname: "tower", port: "8080" };
const PANEL_TLS = { protocol: "https:", hostname: "mcpanel.home.example", port: "" };

describe("classifyConsoleLink", () => {
  it("the panel's host name on another port (or scheme) is the dangerous kind", () => {
    expect(classifyConsoleLink("http://tower:25565/crash-report", PANEL)).toEqual({
      url: "http://tower:25565/crash-report",
      kind: "panel-other-port",
      host: "tower",
      port: "25565",
    });
    // the default port of the scheme counts as a port too, and host names ignore case and a final dot
    expect(classifyConsoleLink("http://TOWER./x", PANEL)?.kind).toBe("panel-other-port");
    expect(classifyConsoleLink("http://mcpanel.home.example/", PANEL_TLS)).toMatchObject({ kind: "panel-other-port", port: "80" });
    expect(classifyConsoleLink("https://mcpanel.home.example:8443/", PANEL_TLS)?.kind).toBe("panel-other-port");
  });

  it("the panel itself and other hosts", () => {
    expect(classifyConsoleLink("http://tower:8080/servers", PANEL)?.kind).toBe("same-origin");
    expect(classifyConsoleLink("https://mcpanel.home.example:443/a", PANEL_TLS)?.kind).toBe("same-origin");
    expect(classifyConsoleLink("https://modrinth.com/mod/sodium", PANEL)).toMatchObject({ kind: "external", host: "modrinth.com", port: "443" });
    // another name of the same machine does not get the panel's cookies
    expect(classifyConsoleLink("http://192.168.1.10:25565/", PANEL)?.kind).toBe("external");
  });

  it("opens only http(s) URLs, normalised as the browser will request them", () => {
    expect(classifyConsoleLink("javascript:alert(1)", PANEL)).toBeNull();
    expect(classifyConsoleLink("file:///etc/passwd", PANEL)).toBeNull();
    expect(classifyConsoleLink("not a url", PANEL)).toBeNull();
    expect(classifyConsoleLink("https://xn--pnel-1na.example/", PANEL)?.url).toBe("https://xn--pnel-1na.example/");
    expect(classifyConsoleLink("https://pänel.example/a b", PANEL)?.url).toBe("https://xn--pnel-loa.example/a%20b");
  });
});

describe("ConsoleLinkDialog", () => {
  afterEach(() => vi.restoreAllMocks());

  function show(link: ConsoleLink) {
    const onClose = vi.fn();
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<ConsoleLinkDialog link={link} onClose={onClose} />);
    return { onClose, open, user: userEvent.setup() };
  }

  it("shows the whole URL and opens it only on confirmation", async () => {
    const { open, onClose, user } = show(classifyConsoleLink("https://example.org/a/very/long/path?with=query#frag", PANEL)!);
    const dialog = screen.getByRole("alertdialog", { name: "Open a link from the console?" });
    expect(within(dialog).getByLabelText("Link address")).toHaveTextContent("https://example.org/a/very/long/path?with=query#frag");
    expect(open).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Open link" }));
    expect(open).toHaveBeenCalledWith("https://example.org/a/very/long/path?with=query#frag", "_blank", "noopener,noreferrer");
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("warns strongly about the panel's own address on another port; Cancel opens nothing", async () => {
    const { open, onClose, user } = show(classifyConsoleLink("http://tower:25565/crash-report", PANEL)!);
    const dialog = screen.getByRole("alertdialog", { name: "This link goes to the panel's address on another port" });
    expect(within(dialog).getByText(/could take over your panel session/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Do not open it unless you know what runs on port 25565/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Open link" })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalled();
    expect(open).not.toHaveBeenCalled();
  });

  it("renders nothing without a link", () => {
    render(<ConsoleLinkDialog link={null} onClose={() => {}} />);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});
