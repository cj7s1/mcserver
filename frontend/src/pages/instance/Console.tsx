import "@xterm/xterm/css/xterm.css";
import { FitAddon } from "@xterm/addon-fit";
import { SearchAddon, type ISearchOptions } from "@xterm/addon-search";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { Terminal } from "@xterm/xterm";
import {
  ArrowDownToLine,
  ChevronDown,
  ChevronUp,
  Copy,
  Download,
  Eraser,
  Filter,
  History,
  Loader2,
  Lock,
  Search,
  X,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { EmptyState } from "@/components/States";
import { statusLabel } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput, InputGroupText } from "@/components/ui/input-group";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useTheme } from "@/hooks/use-theme";
import { api, errorMessage } from "@/lib/api";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";
import { consoleRules, gameId, isMinecraft } from "@/lib/games";
import { useInstanceContext } from "./InstanceLayout";
import { useGameInfo } from "./game-api";
import { CommandInput } from "./console/CommandInput";
import { ConnectionIndicator } from "./console/ConnectionIndicator";
import {
  entryText,
  isFiltering,
  makeEntry,
  matches,
  mergeHistory,
  type ConsoleEntry,
  type ConsoleFilter,
  type LevelFilter,
} from "./console/console-ansi";
import { PendingCommands } from "./console/pending-commands";
import { SEARCH_DECORATIONS, TERMINAL_THEMES } from "./console/terminal-theme";
import { useConsoleSocket, type ConsoleLocalEvent, type ConsoleServerMessage } from "./console/use-console-socket";
import { COMMAND_STATUSES, copyText } from "./overview/overview-utils";

/** Lines kept in the client-side buffer (and therefore available to filters, copy and download). */
const BUFFER_CAP = 5000;
const HISTORY_TAIL = 2000;
/** RIS (full terminal reset) */
const RIS = "\x1bc";

const CONTAINER_STATES: Record<string, string> = {
  running: "running",
  exited: "stopped",
  created: "created",
  restarting: "restarting",
  paused: "paused",
  dead: "dead",
  removing: "removing",
  missing: "none", // a stopped server has no container
};

function IconAction({ label, onClick, disabled, children }: { label: string; onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button type="button" variant="ghost" size="icon-sm" onClick={onClick} disabled={disabled} aria-label={label}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

function slug(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "server";
}

function ConsoleView({ instance, canCommandPerm, canFiles }: { instance: Instance; canCommandPerm: boolean; canFiles: boolean }) {
  // the game's console rules (stop words, leading slash); Minecraft keeps the built-in ones
  const minecraft = isMinecraft(instance);
  const gameInfo = useGameInfo(minecraft ? null : instance);
  const { resolved } = useTheme();
  const themeRef = useRef(resolved);
  const hostRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const searchRef = useRef<SearchAddon | null>(null);
  const entriesRef = useRef<ConsoleEntry[]>([]);
  const filterRef = useRef<ConsoleFilter>({ text: "", level: "all" });
  const pendingRef = useRef(new PendingCommands());
  const seqRef = useRef(0);
  const atBottomRef = useRef(true);
  const containerStateRef = useRef<string | null>(null);
  const countsTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const searchTermRef = useRef("");

  const [filterInput, setFilterInput] = useState("");
  const [level, setLevel] = useState<LevelFilter>("all");
  const [searchTerm, setSearchTerm] = useState("");
  const [searchInfo, setSearchInfo] = useState<{ index: number; count: number } | null>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [unseen, setUnseen] = useState(0);
  const [counts, setCounts] = useState({ total: 0, shown: 0 });
  const [pending, setPending] = useState(0);
  const [historyBusy, setHistoryBusy] = useState(false);

  // --- buffer → terminal ------------------------------------------------------------
  // These helpers only touch refs and state setters, so the versions captured by the
  // mount-time effects stay valid for the component's lifetime.
  const scheduleCounts = () => {
    if (countsTimer.current) return;
    countsTimer.current = setTimeout(() => {
      countsTimer.current = undefined;
      const f = filterRef.current;
      const all = entriesRef.current;
      setCounts({ total: all.length, shown: isFiltering(f) ? all.reduce((n, e) => n + (matches(e, f) ? 1 : 0), 0) : all.length });
    }, 200);
  };

  const toText = (list: ConsoleEntry[]) => (list.length ? `${list.map((e) => e.rendered).join("\r\n")}\r\n` : "");

  const writeEntries = (list: ConsoleEntry[]) => {
    if (list.length) termRef.current?.write(toText(list));
  };

  /** Redraw the terminal from the buffer (after a filter change, clear, reset or history load). */
  const renderAll = () => {
    const term = termRef.current;
    if (term) {
      searchRef.current?.clearDecorations();
      setSearchInfo(null);
      const f = filterRef.current;
      const list = isFiltering(f) ? entriesRef.current.filter((e) => matches(e, f)) : entriesRef.current;
      // Reset with RIS (ESC c) through the write queue rather than term.reset(), so writes
      // still pending in xterm's async buffer cannot land after the redraw.
      term.write(`${RIS}${toText(list)}`, () => {
        if (termRef.current !== term) return;
        term.scrollToBottom();
        // keep an active search highlighted after the redraw (jumps to the newest match)
        const q = searchTermRef.current;
        if (q && searchRef.current) {
          searchRef.current.findPrevious(q, { caseSensitive: false, decorations: SEARCH_DECORATIONS[themeRef.current] });
        }
      });
    }
    setUnseen(0);
    scheduleCounts();
  };

  const append = (list: ConsoleEntry[]) => {
    if (list.length === 0) return;
    const buf = entriesRef.current;
    for (const e of list) buf.push(e);
    if (buf.length > BUFFER_CAP) buf.splice(0, buf.length - BUFFER_CAP);
    const f = filterRef.current;
    const visible = isFiltering(f) ? list.filter((e) => matches(e, f)) : list;
    writeEntries(visible);
    if (!atBottomRef.current && visible.length) setUnseen((u) => u + visible.length);
    scheduleCounts();
  };

  const sys = (text: string) => makeEntry("sys", text);

  // --- WebSocket --------------------------------------------------------------------
  const onEvent = (msg: ConsoleServerMessage | ConsoleLocalEvent) => {
    switch (msg.type) {
      case "_open":
        if (msg.reconnect) {
          // a new subscription re-sends the log tail; start from a clean buffer
          entriesRef.current = [];
          renderAll();
          append([sys("reconnected · log tail reloaded")]);
        }
        break;
      case "_disconnected":
        if (pendingRef.current.size) {
          append(pendingRef.current.drop().map((c) => makeEntry("err", `no result for "${c}": console disconnected`)));
          setPending(0);
        }
        break;
      case "reset":
        entriesRef.current = [];
        renderAll();
        append([sys("node agent reconnected · log tail reloaded")]);
        break;
      case "logs":
        append(msg.lines.map((l) => makeEntry("log", l)));
        break;
      case "log":
        append([makeEntry("log", msg.line)]);
        break;
      case "state": {
        const prev = containerStateRef.current;
        containerStateRef.current = msg.state;
        if (prev && prev !== msg.state) {
          // a stopped server has no container; the end of its last run stays in this view
          append([sys(msg.state === "missing" ? "container removed (the server is stopped)" : `container ${CONTAINER_STATES[msg.state] ?? msg.state}`)]);
        }
        break;
      }
      case "error":
        append([makeEntry("err", msg.message || "Log stream error")]);
        break;
      case "closed":
        append([sys("log stream ended · reconnecting")]);
        break;
      case "notice": {
        // A note from the panel (e.g. "stop" was turned into a Stop of the server, or refused). With
        // an id it answers that command: the wait ends here, and the command_result that follows
        // with the same text is not shown again (PendingCommands).
        if (pendingRef.current.notice(msg.id)) setPending(pendingRef.current.size);
        const text = typeof msg.message === "string" ? msg.message : typeof msg.text === "string" ? msg.text : "";
        if (text) append([makeEntry(msg.level === "error" ? "err" : "notice", text)]);
        break;
      }
      case "command_result": {
        const shown = pendingRef.current.result(msg.id);
        setPending(pendingRef.current.size);
        if (shown === "skip") break;
        if (msg.ok) {
          const lines = (msg.output ?? "").replace(/\r/g, "").split("\n");
          while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
          append(lines.length ? lines.map((l) => makeEntry("out", l)) : [makeEntry("out", "\x1b[2m(no output)")]);
        } else {
          append([makeEntry("err", msg.error || "Command failed")]);
        }
        break;
      }
    }
  };

  const sock = useConsoleSocket(instance.id, onEvent);

  // --- terminal lifecycle -------------------------------------------------------------
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const term = new Terminal({
      allowProposedApi: true, // search result decorations
      disableStdin: true,
      convertEol: false,
      cursorBlink: false,
      cursorStyle: "bar",
      cursorInactiveStyle: "none",
      fontFamily: 'ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace',
      fontSize: 12.5,
      lineHeight: 1.25,
      scrollback: BUFFER_CAP + 500,
      overviewRuler: { width: 8 },
      theme: TERMINAL_THEMES[themeRef.current],
    });
    const fit = new FitAddon();
    const search = new SearchAddon({ highlightLimit: 2000 });
    term.loadAddon(fit);
    term.loadAddon(search);
    term.loadAddon(
      new WebLinksAddon((event, uri) => {
        event.preventDefault();
        window.open(uri, "_blank", "noopener,noreferrer");
      }),
    );
    term.open(host);
    termRef.current = term;
    searchRef.current = search;

    const doFit = () => {
      try {
        if (host.clientWidth > 0 && host.clientHeight > 0) fit.fit();
      } catch {
        /* not measurable yet */
      }
    };
    doFit();

    const checkBottom = () => {
      const b = term.buffer.active;
      const bottom = b.viewportY >= b.baseY;
      if (bottom !== atBottomRef.current) {
        atBottomRef.current = bottom;
        setAtBottom(bottom);
        if (bottom) setUnseen(0);
      }
    };
    const subs = [
      term.onScroll(checkBottom),
      term.onWriteParsed(checkBottom),
      search.onDidChangeResults((r) => setSearchInfo({ index: r.resultIndex, count: r.resultCount })),
    ];
    term.attachCustomKeyEventHandler((e) => {
      if (e.type !== "keydown") return true;
      const mod = e.ctrlKey || e.metaKey;
      const key = e.key.toLowerCase();
      if (mod && key === "c" && term.hasSelection()) return false; // let the browser copy the selection
      if (mod && key === "f") {
        e.preventDefault();
        searchInputRef.current?.focus();
        return false;
      }
      return true;
    });

    const frame = { id: 0 };
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(frame.id);
      frame.id = requestAnimationFrame(doFit);
    });
    ro.observe(host);

    // replay anything buffered before the terminal existed (or across a StrictMode remount)
    renderAll();

    return () => {
      ro.disconnect();
      cancelAnimationFrame(frame.id);
      subs.forEach((d) => d.dispose());
      term.dispose();
      termRef.current = null;
      searchRef.current = null;
    };
  }, []);

  useEffect(
    () => () => {
      clearTimeout(countsTimer.current);
      countsTimer.current = undefined;
    },
    [],
  );

  // theme follows the panel's light/dark mode
  useEffect(() => {
    themeRef.current = resolved;
    if (termRef.current) termRef.current.options.theme = TERMINAL_THEMES[resolved];
  }, [resolved]);

  // client-side filter: rebuild the terminal from the buffer when it changes
  useEffect(() => {
    const next: ConsoleFilter = { text: filterInput.trim().toLowerCase(), level };
    const cur = filterRef.current;
    if (cur.text === next.text && cur.level === next.level) return;
    const t = setTimeout(
      () => {
        filterRef.current = next;
        renderAll();
      },
      next.text !== cur.text ? 150 : 0,
    );
    return () => clearTimeout(t);
  }, [filterInput, level]);

  // --- actions ------------------------------------------------------------------------
  const searchOpts = (incremental = false): ISearchOptions => ({
    caseSensitive: false,
    incremental,
    decorations: SEARCH_DECORATIONS[resolved],
  });

  const find = (dir: "next" | "prev", term = searchTerm, incremental = false) => {
    const s = searchRef.current;
    if (!s) return;
    if (!term) {
      s.clearDecorations();
      setSearchInfo(null);
      return;
    }
    const found = dir === "next" ? s.findNext(term, searchOpts(incremental)) : s.findPrevious(term, searchOpts());
    if (!found) setSearchInfo({ index: -1, count: 0 });
  };

  const visibleText = () => {
    const f = filterRef.current;
    return entriesRef.current.filter((e) => matches(e, f)).map(entryText);
  };

  const copyVisible = async () => {
    const lines = visibleText();
    if (!lines.length) {
      toast.info("Nothing to copy");
      return;
    }
    if (await copyText(lines.join("\n"))) toast.success(`Copied ${lines.length.toLocaleString()} lines`);
    else toast.error("Could not access the clipboard. Use Download instead.");
  };

  const downloadVisible = () => {
    const lines = visibleText();
    if (!lines.length) {
      toast.info("Nothing to download");
      return;
    }
    const blob = new Blob([`${lines.join("\n")}\n`], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const stamp = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
    a.href = url;
    a.download = `${slug(instance.name)}-console-${stamp}.log`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const clearView = () => {
    entriesRef.current = [];
    renderAll();
  };

  const loadHistory = async () => {
    setHistoryBusy(true);
    try {
      const r = await api.get<{ lines: string[] }>(`/instances/${instance.id}/logs`, { tail: HISTORY_TAIL });
      const merged = mergeHistory(r.lines ?? [], entriesRef.current);
      entriesRef.current = merged.length > BUFFER_CAP ? merged.slice(-BUFFER_CAP) : merged;
      renderAll();
      toast.success(`Loaded ${(r.lines ?? []).length.toLocaleString()} lines of console history`);
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setHistoryBusy(false);
    }
  };

  const sendCommand = (command: string): boolean => {
    const id = `${Date.now().toString(36)}-${++seqRef.current}`;
    if (!sock.send({ type: "command", id, command })) {
      toast.error("The console is not connected. Wait for it to reconnect and try again.");
      return false;
    }
    pendingRef.current.add(id, command);
    setPending(pendingRef.current.size);
    append([makeEntry("cmd", command)]);
    termRef.current?.scrollToBottom();
    return true;
  };

  // --- derived UI state ------------------------------------------------------------------
  const socketOpen = sock.state === "connected" || sock.state === "agent_lost";
  let disabledReason: string | null = null;
  if (!canCommandPerm || sock.canCommand === false) disabledReason = "You can view the console but do not have permission to send commands.";
  else if (!COMMAND_STATUSES.includes(instance.status))
    disabledReason = `Commands can be sent while the server is running (it is currently ${statusLabel(instance.status).toLowerCase()}).`;
  else if (!socketOpen) disabledReason = "The console is not connected yet; commands are available once it reconnects.";

  const filtering = !!filterInput.trim() || level !== "all";
  const containerLabel = sock.containerState ? (CONTAINER_STATES[sock.containerState] ?? sock.containerState) : null;
  const empty = counts.total === 0;

  return (
    <div className="grid min-w-0 gap-2">
      <div className="flex h-[calc(100svh-15rem)] min-h-[440px] flex-col overflow-hidden rounded-xl border bg-card">
        {/* status + actions */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b px-3 py-2">
          <ConnectionIndicator state={sock.state} agentMessage={sock.agentMessage} retryAt={sock.retryAt} onRetry={sock.reconnectNow} />
          {containerLabel && (
            <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">container: {containerLabel}</span>
          )}
          <span className="text-xs text-muted-foreground tabular-nums" aria-live="off">
            {filtering ? `${counts.shown.toLocaleString()} of ${counts.total.toLocaleString()} lines` : `${counts.total.toLocaleString()} lines`}
          </span>
          <div className="ml-auto flex items-center gap-0.5">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button type="button" variant="ghost" size="sm" onClick={loadHistory} disabled={historyBusy}>
                  {historyBusy ? <Loader2 className="animate-spin" /> : <History />}
                  <span className="hidden sm:inline">Load history</span>
                  <span className="sr-only sm:hidden">Load history</span>
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                Fetch the last {HISTORY_TAIL.toLocaleString()} lines of console output (for a stopped server: of its last run)
              </TooltipContent>
            </Tooltip>
            <IconAction label={filtering ? "Copy visible (filtered) lines" : "Copy visible lines"} onClick={copyVisible} disabled={empty}>
              <Copy />
            </IconAction>
            <IconAction label={filtering ? "Download visible (filtered) lines" : "Download visible lines"} onClick={downloadVisible} disabled={empty}>
              <Download />
            </IconAction>
            <IconAction label="Clear view (does not affect the server)" onClick={clearView} disabled={empty}>
              <Eraser />
            </IconAction>
          </div>
        </div>

        {/* filter + search */}
        <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
          <InputGroup className="w-full sm:w-60">
            <InputGroupAddon>
              <Filter aria-hidden />
            </InputGroupAddon>
            <InputGroupInput
              value={filterInput}
              onChange={(e) => setFilterInput(e.target.value)}
              placeholder="Filter lines…"
              aria-label="Show only lines containing"
              spellCheck={false}
            />
            {filterInput && (
              <InputGroupAddon align="inline-end">
                <InputGroupButton size="icon-xs" aria-label="Clear filter" onClick={() => setFilterInput("")}>
                  <X />
                </InputGroupButton>
              </InputGroupAddon>
            )}
          </InputGroup>
          <ToggleGroup
            type="single"
            size="sm"
            variant="outline"
            spacing={0}
            value={level}
            onValueChange={(v) => v && setLevel(v as LevelFilter)}
            aria-label="Log level"
          >
            <ToggleGroupItem value="all" aria-label="All lines">
              All
            </ToggleGroupItem>
            <ToggleGroupItem value="warn" aria-label="Warnings and errors">
              Warn+
            </ToggleGroupItem>
            <ToggleGroupItem value="error" aria-label="Errors only">
              Errors only
            </ToggleGroupItem>
          </ToggleGroup>
          <InputGroup className="w-full sm:ml-auto sm:w-64">
            <InputGroupAddon>
              <Search aria-hidden />
            </InputGroupAddon>
            <InputGroupInput
              ref={searchInputRef}
              value={searchTerm}
              onChange={(e) => {
                setSearchTerm(e.target.value);
                searchTermRef.current = e.target.value;
                find("next", e.target.value, true);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  find(e.shiftKey ? "prev" : "next");
                } else if (e.key === "Escape") {
                  setSearchTerm("");
                  searchTermRef.current = "";
                  find("next", "");
                }
              }}
              onBlur={() => searchRef.current?.clearActiveDecoration()}
              placeholder="Search output…"
              title="Enter: next match · Shift+Enter: previous · Esc: clear"
              aria-label="Search console output"
              spellCheck={false}
            />
            <InputGroupAddon align="inline-end">
              {searchInfo && searchTerm && (
                <InputGroupText className="text-xs tabular-nums" aria-live="polite">
                  {searchInfo.count === 0 ? "No matches" : searchInfo.index >= 0 ? `${searchInfo.index + 1}/${searchInfo.count}` : `${searchInfo.count}+`}
                </InputGroupText>
              )}
              <InputGroupButton size="icon-xs" aria-label="Previous match" onClick={() => find("prev")} disabled={!searchTerm}>
                <ChevronUp />
              </InputGroupButton>
              <InputGroupButton size="icon-xs" aria-label="Next match" onClick={() => find("next")} disabled={!searchTerm}>
                <ChevronDown />
              </InputGroupButton>
            </InputGroupAddon>
          </InputGroup>
        </div>

        {/* terminal */}
        <div className="relative min-h-0 flex-1" style={{ background: TERMINAL_THEMES[resolved].background }}>
          <div
            ref={hostRef}
            className="absolute inset-0 overflow-hidden [&_.xterm-viewport]:bg-transparent!"
            role="log"
            aria-label="Server console output"
          />
          {empty && (
            <div className="pointer-events-none absolute inset-0 grid place-items-center p-6 text-center text-sm text-muted-foreground">
              {sock.state === "connecting"
                ? "Connecting to the console…"
                : sock.containerState === "missing"
                  ? "No output yet. It appears here once the server is installed and started."
                  : "Waiting for output…"}
            </div>
          )}
          {!empty && filtering && counts.shown === 0 && (
            <div className="pointer-events-none absolute inset-x-0 top-6 text-center text-sm text-muted-foreground">No lines match the current filter.</div>
          )}
          {!atBottom && (
            <div className="absolute right-4 bottom-3 flex items-center gap-2 rounded-lg border bg-popover/95 py-1 pr-1 pl-3 text-xs text-popover-foreground shadow-md backdrop-blur">
              <span>
                Autoscroll paused
                {unseen > 0 && <span className="text-muted-foreground"> · {unseen.toLocaleString()} new</span>}
              </span>
              <Button type="button" size="xs" onClick={() => termRef.current?.scrollToBottom()}>
                <ArrowDownToLine /> Jump to latest
              </Button>
            </div>
          )}
        </div>

        {/* command line */}
        <div className="border-t p-3">
          <CommandInput
            instanceId={instance.id}
            disabledReason={disabledReason}
            pending={pending}
            onSubmit={sendCommand}
            rules={minecraft ? undefined : consoleRules(gameInfo ?? { id: gameId(instance) })}
          />
        </div>
      </div>
      <p className="px-1 text-xs text-muted-foreground">
        The console opens with the last 300 lines of output; <strong className="font-medium">Load history</strong> fetches up to{" "}
        {HISTORY_TAIL.toLocaleString()}. While the server is stopped this is the end of its last run, which also stays in front of the
        next run&apos;s output. The view keeps the newest {BUFFER_CAP.toLocaleString()} lines. Older output is in the server&apos;s own
        log files under{" "}
        {canFiles ? (
          <Link to={`/servers/${instance.id}/files?path=logs`} className={cn("font-mono text-primary hover:underline")}>
            Files → logs/
          </Link>
        ) : (
          <span className="font-mono">logs/</span>
        )}
        .
      </p>
    </div>
  );
}

export default function Console() {
  const { instance, can } = useInstanceContext();
  if (!can("instance.console")) {
    return (
      <EmptyState
        icon={<Lock className="size-5" />}
        title="No console access"
        description="Viewing this server's live output requires the Console permission. Ask an administrator to grant it on the Access tab."
      />
    );
  }
  return <ConsoleView key={instance.id} instance={instance} canCommandPerm={can("instance.command")} canFiles={can("instance.files.read")} />;
}
