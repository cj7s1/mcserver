/**
 * Command palette (Ctrl+K / Cmd+K or the search button in the top bar) and global keyboard shortcuts.
 *
 * Offers navigation, every server the user can see with its pages, permission-aware power actions
 * (Start runs like the existing Start buttons; Stop and Restart always go through ConfirmDialog),
 * theme switching and the shortcuts help ("?"). Power actions reuse `useServerPower`, i.e. the same
 * `POST /instances/<id>/power` call, toasts and cache invalidation as the server library buttons.
 */
import { Command as CommandPrimitive, useCommandState } from "cmdk";
import { Keyboard, Monitor, Search, SunMoon, UserRound } from "lucide-react";
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { matchPath, useLocation, useNavigate } from "react-router";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandShortcut,
} from "@/components/ui/command";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { useAuth } from "@/hooks/use-auth";
import { useTheme } from "@/hooks/use-theme";
import { errorMessage } from "@/lib/api";
import { useInstance, useInstances } from "@/lib/queries";
import type { Instance } from "@/lib/types";
import { useServerPower, type PowerAction } from "@/pages/library/server-utils";
import {
  navEntries,
  powerConfirmCopy,
  powerItemText,
  powerOptions,
  serverEntry,
  serverSectionEntries,
  type PaletteEntry,
  type PaletteNavGroup,
} from "./entries";
import { fuzzyScore, orderGroups } from "./fuzzy";
import { Kbd, KeyCaps } from "./Kbd";
import { ShortcutsHelp } from "./ShortcutsHelp";
import {
  isMacPlatform,
  PALETTE_ARIA_KEYSHORTCUTS,
  paletteHotkeyCaps,
  SHORTCUTS,
  shortcutFor,
  type ShortcutId,
} from "./shortcuts";
import { pushRecent, readRecent, readSingleKeyShortcuts, writeSingleKeyShortcuts } from "./storage";
import { useGlobalShortcuts } from "./use-global-shortcuts";

export type { PaletteNavGroup, PaletteNavItem } from "./entries";

interface PowerRequest {
  instance: Instance;
  action: Exclude<PowerAction, "start">;
}

const ACCOUNT_ENTRY: PaletteEntry = {
  id: "nav:/account",
  label: "Account & password",
  keywords: ["Account & password", "password", "profile", "me", "account"],
  icon: <UserRound />,
  to: "/account",
};

/** Taller rows on touch screens, the regular density from sm up. */
const ITEM_CLASS = "py-2.5 sm:py-1.5";

const paletteFilter = (_value: string, search: string, keywords?: string[]) => fuzzyScore(search, keywords);

export function CommandPalette({ navigation }: { navigation: readonly PaletteNavGroup[] }) {
  const [open, setOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [confirm, setConfirm] = useState<PowerRequest | null>(null);
  const [singleKey, setSingleKey] = useState(() => readSingleKeyShortcuts());
  // Work to do once the palette has closed and handed focus back (opening another dialog earlier
  // would let the palette's focus restore fight the new dialog's focus trap).
  const afterClose = useRef<(() => void) | null>(null);
  const navigate = useNavigate();
  const { user } = useAuth();
  const mac = useMemo(() => isMacPlatform(), []);

  // Recomputed only when the permission set changes (the listener is re-registered with it).
  const permsKey = user?.permissions.join(",") ?? "";
  const shortcuts = useMemo(
    () => SHORTCUTS.filter((s) => !s.perm || permsKey.split(",").includes(s.perm)),
    [permsKey],
  );

  const runShortcut = useCallback(
    (id: ShortcutId) => {
      if (id === "help") {
        setHelpOpen(true);
        return;
      }
      const def = shortcuts.find((s) => s.id === id);
      if (def?.to) navigate(def.to);
    },
    [navigate, shortcuts],
  );

  useGlobalShortcuts({
    paletteOpen: open,
    onTogglePalette: () => setOpen((o) => !o),
    singleKeyEnabled: singleKey,
    bindings: shortcuts,
    onShortcut: runShortcut,
  });

  const closeThen = (fn: () => void) => {
    afterClose.current = fn;
    setOpen(false);
  };

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen(true)}
        aria-label="Search pages, servers and actions"
        aria-haspopup="dialog"
        aria-keyshortcuts={PALETTE_ARIA_KEYSHORTCUTS}
        title={`Search (${paletteHotkeyCaps(mac).join(mac ? "" : "+")})`}
        className="size-8 justify-center px-0 text-muted-foreground sm:h-8 sm:w-56 sm:justify-start sm:px-2.5 lg:w-64"
      >
        <Search />
        <span className="hidden flex-1 text-left font-normal sm:inline">Search…</span>
        <KeyCaps keys={paletteHotkeyCaps(mac)} className="hidden sm:inline-flex" />
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          showCloseButton={false}
          className="top-[8svh] translate-y-0 gap-0 overflow-hidden p-0 sm:top-[14vh] sm:max-w-xl motion-reduce:animate-none!"
          onCloseAutoFocus={() => {
            const fn = afterClose.current;
            afterClose.current = null;
            fn?.();
          }}
        >
          <DialogTitle className="sr-only">Command palette</DialogTitle>
          <DialogDescription className="sr-only">
            Search pages, servers and actions. Use the up and down arrow keys to move and Enter to open or run.
          </DialogDescription>
          <PaletteBody
            navigation={navigation}
            mac={mac}
            shortcutsEnabled={singleKey}
            onNavigate={(entry) => {
              pushRecent(entry.id);
              setOpen(false);
              navigate(entry.to);
            }}
            onConfirm={(req) => closeThen(() => setConfirm(req))}
            onShowHelp={() => closeThen(() => setHelpOpen(true))}
            onClose={() => setOpen(false)}
          />
        </DialogContent>
      </Dialog>

      <ShortcutsHelp
        open={helpOpen}
        onOpenChange={setHelpOpen}
        mac={mac}
        shortcuts={shortcuts}
        singleKeyEnabled={singleKey}
        onSingleKeyChange={(enabled) => {
          setSingleKey(enabled);
          writeSingleKeyShortcuts(enabled);
        }}
      />

      {confirm && <PowerConfirm key={`${confirm.instance.id}:${confirm.action}`} request={confirm} onClose={() => setConfirm(null)} />}
    </>
  );
}

/** Stop/Restart confirmation; the request goes through the same hook as the library power buttons. */
function PowerConfirm({ request, onClose }: { request: PowerRequest; onClose: () => void }) {
  const power = useServerPower(request.instance);
  const copy = powerConfirmCopy(request.instance, request.action);
  return (
    <ConfirmDialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title={copy.title}
      description={copy.description}
      confirmLabel={copy.confirmLabel}
      destructive={copy.destructive}
      onConfirm={() => power.mutateAsync(request.action)}
    />
  );
}

function PaletteBody({
  navigation,
  mac,
  shortcutsEnabled,
  onNavigate,
  onConfirm,
  onShowHelp,
  onClose,
}: {
  navigation: readonly PaletteNavGroup[];
  mac: boolean;
  shortcutsEnabled: boolean;
  onNavigate: (entry: PaletteEntry) => void;
  onConfirm: (req: PowerRequest) => void;
  onShowHelp: () => void;
  onClose: () => void;
}) {
  const [search, setSearch] = useState("");
  const searching = search.trim().length > 0;
  const listRef = useRef<HTMLDivElement>(null);
  // New results start at the top (cmdk selects the first one); keep the list from staying scrolled.
  useLayoutEffect(() => {
    if (listRef.current) listRef.current.scrollTop = 0;
  }, [search]);
  const { can } = useAuth();
  const { resolved, theme, setTheme } = useTheme();
  const location = useLocation();
  const active = useInstances();
  const archived = useInstances("true");

  const routeId = matchPath("/servers/:id/*", location.pathname)?.params.id;
  const currentId = routeId && routeId !== "new" ? routeId : undefined;
  const currentQuery = useInstance(currentId);

  const instances = useMemo(() => {
    const seen = new Set<string>();
    return [...(active.data ?? []), ...(archived.data ?? [])].filter((i) => (seen.has(i.id) ? false : (seen.add(i.id), true)));
  }, [active.data, archived.data]);
  const current = currentId ? (currentQuery.data ?? instances.find((i) => i.id === currentId)) : undefined;
  const others = current ? instances.filter((i) => i.id !== current.id) : instances;

  const navGroups = navEntries(navigation, can);
  if (navGroups.length) navGroups[0] = { ...navGroups[0], entries: [...navGroups[0].entries, ACCOUNT_ENTRY] };

  const [recentIds] = useState(() => readRecent());
  // Recents are stored as ids and resolved against what the user can open right now, so a server
  // that was deleted or a page whose permission was revoked simply drops out.
  const byId = new Map<string, PaletteEntry>();
  for (const g of navGroups) for (const e of g.entries) byId.set(e.id, e);
  for (const i of instances) {
    for (const e of serverSectionEntries(i)) byId.set(e.id, e);
    const s = serverEntry(i);
    byId.set(s.id, s);
  }
  const recents = recentIds.map((id) => byId.get(id)).filter((e): e is PaletteEntry => !!e);

  const shortcutHint = (to: string) => {
    const s = shortcutsEnabled ? shortcutFor(to) : undefined;
    return s ? <KeyCaps keys={s.keys} sequence={s.keys.length > 1} /> : null;
  };

  const loadError = active.error ?? archived.error;

  // Each group carries the keywords of its items so groups can be ordered by their best match:
  // cmdk sorts items inside a group, but does not reorder groups itself.
  const groups: { key: string; keywords: string[][]; node: ReactNode }[] = [];
  if (current) {
    const pages = serverSectionEntries(current, { primaryOnly: !searching }).map((e) => ({ ...e, context: undefined }));
    groups.push({
      key: "here",
      keywords: [...powerOptions(current).map((o) => powerItemText(current, o, true).keywords), ...pages.map((e) => e.keywords)],
      node: (
        <CommandGroup heading={`Current server · ${current.name}`}>
          <ServerPowerItems instance={current} valuePrefix="here" contextual onConfirm={onConfirm} onDone={onClose} />
          {pages.map((e) => (
            <EntryItem key={e.id} entry={e} value={`here:${e.id}`} onSelect={onNavigate} />
          ))}
        </CommandGroup>
      ),
    });
  }
  if (!searching && recents.length > 0) {
    groups.push({
      key: "recent",
      keywords: [],
      node: (
        <CommandGroup heading="Recent">
          {recents.map((e) => (
            <EntryItem key={e.id} entry={e} value={`recent:${e.id}`} onSelect={onNavigate} />
          ))}
        </CommandGroup>
      ),
    });
  }
  for (const g of navGroups) {
    groups.push({
      key: `nav:${g.heading}`,
      keywords: g.entries.map((e) => e.keywords),
      node: (
        <CommandGroup heading={g.heading}>
          {g.entries.map((e) => (
            <EntryItem key={e.id} entry={e} value={e.id} onSelect={onNavigate} shortcut={shortcutHint(e.to)} />
          ))}
        </CommandGroup>
      ),
    });
  }
  const serverEntries = instances.map(serverEntry);
  groups.push({
    key: "servers",
    keywords: serverEntries.map((e) => e.keywords),
    node: (
      <CommandGroup heading="Servers">
        {serverEntries.map((e, idx) => (
          <EntryItem key={e.id} entry={e} value={`servers:${instances[idx].id}`} onSelect={onNavigate} />
        ))}
      </CommandGroup>
    ),
  });
  if (searching && others.length > 0) {
    const pages = others.flatMap((i) => serverSectionEntries(i).filter((e) => !e.id.endsWith(":overview")));
    groups.push({
      key: "pages",
      keywords: pages.map((e) => e.keywords),
      node: (
        <CommandGroup heading="Server pages">
          {pages.map((e) => (
            <EntryItem key={e.id} entry={e} value={`page:${e.id}`} onSelect={onNavigate} />
          ))}
        </CommandGroup>
      ),
    });
    groups.push({
      key: "actions",
      keywords: others.flatMap((i) => powerOptions(i).map((o) => powerItemText(i, o, false).keywords)),
      node: (
        <CommandGroup heading="Server actions">
          {others.map((i) => (
            <ServerPowerItems key={i.id} instance={i} valuePrefix={`action:${i.id}`} onConfirm={onConfirm} onDone={onClose} />
          ))}
        </CommandGroup>
      ),
    });
  }
  const themeKeywords = ["Toggle theme", resolved === "dark" ? "light mode" : "dark mode", "appearance", "colors"];
  const systemKeywords = ["Use system theme", "appearance", "auto", "os"];
  const helpKeywords = ["Keyboard shortcuts", "help", "keys", "hotkeys"];
  groups.push({
    key: "prefs",
    keywords: [themeKeywords, ...(theme !== "system" ? [systemKeywords] : []), helpKeywords],
    node: (
      <CommandGroup heading="Preferences">
        <CommandItem
          value="pref:theme"
          keywords={themeKeywords}
          onSelect={() => {
            setTheme(resolved === "dark" ? "light" : "dark");
            onClose();
          }}
          className={ITEM_CLASS}
        >
          <SunMoon />
          <span className="flex-1 truncate">Toggle theme</span>
          <span className="text-xs text-muted-foreground">{resolved === "dark" ? "Switch to light" : "Switch to dark"}</span>
        </CommandItem>
        {theme !== "system" && (
          <CommandItem
            value="pref:system-theme"
            keywords={systemKeywords}
            onSelect={() => {
              setTheme("system");
              onClose();
            }}
            className={ITEM_CLASS}
          >
            <Monitor />
            <span className="flex-1 truncate">Use system theme</span>
          </CommandItem>
        )}
        <CommandItem value="pref:help" keywords={helpKeywords} onSelect={onShowHelp} className={ITEM_CLASS}>
          <Keyboard />
          <span className="flex-1 truncate">Keyboard shortcuts</span>
          {shortcutsEnabled && (
            <CommandShortcut>
              <Kbd>?</Kbd>
            </CommandShortcut>
          )}
        </CommandItem>
      </CommandGroup>
    ),
  });
  const ordered = searching ? orderGroups(groups, search) : groups;

  return (
    <Command label="Command palette" filter={paletteFilter} loop className="rounded-none! bg-transparent p-0">
      <CommandInput
        value={search}
        onValueChange={setSearch}
        placeholder="Search pages, servers and actions…"
        className="text-base sm:text-sm"
      />
      <CommandList ref={listRef} className="max-h-[min(26rem,62svh)] p-1">
        <CommandEmpty>No matching pages, servers or actions.</CommandEmpty>
        {ordered.map((g) => (
          <Fragment key={g.key}>{g.node}</Fragment>
        ))}
        {active.isLoading && (
          <CommandPrimitive.Loading>
            <div className="px-3 py-2 text-xs text-muted-foreground">Loading servers…</div>
          </CommandPrimitive.Loading>
        )}
        {loadError && (
          <div role="alert" className="px-3 py-2 text-xs text-destructive">
            Could not load servers: {errorMessage(loadError)}
          </div>
        )}
      </CommandList>
      <ResultCount active={searching} />
      <div className="hidden items-center gap-4 border-t px-3 py-2 text-[11px] text-muted-foreground sm:flex" aria-hidden>
        <span className="inline-flex items-center gap-1">
          <Kbd>↑</Kbd>
          <Kbd>↓</Kbd> move
        </span>
        <span className="inline-flex items-center gap-1">
          <Kbd>↵</Kbd> open
        </span>
        <span className="inline-flex items-center gap-1">
          <Kbd>Esc</Kbd> close
        </span>
        <span className="ml-auto inline-flex items-center gap-1">
          <KeyCaps keys={paletteHotkeyCaps(mac)} /> toggle
        </span>
      </div>
    </Command>
  );
}

function EntryItem({
  entry,
  value,
  onSelect,
  shortcut,
}: {
  entry: PaletteEntry;
  value: string;
  onSelect: (entry: PaletteEntry) => void;
  shortcut?: ReactNode;
}) {
  return (
    <CommandItem value={value} keywords={entry.keywords} onSelect={() => onSelect(entry)} className={ITEM_CLASS}>
      {entry.icon}
      <span className="flex min-w-0 flex-1 items-center gap-1.5">
        {entry.context && (
          <span className="max-w-[50%] shrink-0 truncate text-muted-foreground">
            {entry.context}
            <span aria-hidden> ›</span>
            <span className="sr-only">:</span>
          </span>
        )}
        <span className="truncate">{entry.label}</span>
      </span>
      {entry.hint && <span className="hidden max-w-[45%] shrink-0 truncate text-xs text-muted-foreground sm:inline">{entry.hint}</span>}
      {shortcut && <CommandShortcut className="hidden tracking-normal sm:inline-flex">{shortcut}</CommandShortcut>}
    </CommandItem>
  );
}

/**
 * Start/Stop/Restart for one server. Start runs at once (like the Start buttons elsewhere); Stop and
 * Restart close the palette and open ConfirmDialog.
 */
function ServerPowerItems({
  instance,
  valuePrefix,
  contextual = false,
  onConfirm,
  onDone,
}: {
  instance: Instance;
  valuePrefix: string;
  /** In the "Current server" group the label says "server" instead of repeating the name. */
  contextual?: boolean;
  onConfirm: (req: PowerRequest) => void;
  onDone: () => void;
}) {
  const power = useServerPower(instance);
  return (
    <>
      {powerOptions(instance).map((o) => {
        const { label, keywords } = powerItemText(instance, o, contextual);
        const Icon = o.icon;
        return (
          <CommandItem
            key={o.action}
            value={`${valuePrefix}:${o.action}`}
            keywords={keywords}
            disabled={!!o.disabledReason || power.isPending}
            onSelect={() => {
              if (o.action === "start") {
                power.mutate("start");
                onDone();
              } else {
                onConfirm({ instance, action: o.action });
              }
            }}
            className={ITEM_CLASS}
          >
            <Icon className={o.action === "stop" ? "text-destructive" : undefined} />
            <span className="flex-1 truncate">{label}</span>
            {o.disabledReason ? (
              <span className="max-w-[45%] truncate text-xs text-muted-foreground">{o.disabledReason}</span>
            ) : (
              o.needsConfirm && <span className="hidden text-xs text-muted-foreground sm:inline">asks to confirm</span>
            )}
          </CommandItem>
        );
      })}
    </>
  );
}

/** Polite, debounced result count for screen readers while the user types. */
function ResultCount({ active }: { active: boolean }) {
  const count = useCommandState((s) => s.filtered.count);
  const [announced, setAnnounced] = useState("");
  useEffect(() => {
    if (!active) return;
    const t = window.setTimeout(() => setAnnounced(count === 0 ? "No results" : `${count} result${count === 1 ? "" : "s"}`), 500);
    return () => window.clearTimeout(t);
  }, [active, count]);
  return (
    <div className="sr-only" role="status" aria-live="polite">
      {active ? announced : ""}
    </div>
  );
}
