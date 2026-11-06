import { describe, expect, it } from "vitest";
import {
  acceptsSingleKeyShortcut,
  createKeySequenceMatcher,
  hasOpenOverlay,
  isMacPlatform,
  isPaletteHotkey,
  isTypingTarget,
  SHORTCUTS,
} from "./shortcuts";
import { fuzzyScore, orderGroups, scoreToken } from "./fuzzy";
import { MAX_RECENT, pushRecent, readRecent, readSingleKeyShortcuts, RECENT_KEY, writeSingleKeyShortcuts } from "./storage";

const BINDINGS = SHORTCUTS.map((s) => ({ id: s.id, keys: s.keys }));

describe("createKeySequenceMatcher", () => {
  it("matches single keys and two-key sequences", () => {
    const m = createKeySequenceMatcher(BINDINGS);
    expect(m.feed("?", 0)).toBe("help");
    expect(m.feed("c", 10)).toBe("create-server");
    expect(m.feed("g", 20)).toBeNull();
    expect(m.pending).toBe(true);
    expect(m.feed("d", 300)).toBe("go-dashboard");
    expect(m.pending).toBe(false);
    expect(m.feed("g", 400)).toBeNull();
    expect(m.feed("s", 500)).toBe("go-servers");
  });

  it("drops a sequence that is not completed within the timeout", () => {
    const m = createKeySequenceMatcher(BINDINGS, 1000);
    expect(m.feed("g", 0)).toBeNull();
    expect(m.feed("d", 1500)).toBeNull();
    expect(m.pending).toBe(false);
    // a fresh "g d" still works afterwards
    expect(m.feed("g", 2000)).toBeNull();
    expect(m.feed("d", 2100)).toBe("go-dashboard");
  });

  it("does not re-interpret a key that breaks a started sequence", () => {
    const m = createKeySequenceMatcher(BINDINGS);
    expect(m.feed("g", 0)).toBeNull();
    // "c" alone would open the create wizard, but after "g" it only cancels the sequence
    expect(m.feed("c", 100)).toBeNull();
    expect(m.pending).toBe(false);
    expect(m.feed("c", 200)).toBe("create-server");
  });

  it("ignores modifier keydowns (Shift pressed to type '?') and unknown keys", () => {
    const m = createKeySequenceMatcher(BINDINGS);
    expect(m.feed("g", 0)).toBeNull();
    expect(m.feed("Shift", 50)).toBeNull();
    expect(m.pending).toBe(true);
    expect(m.feed("s", 100)).toBe("go-servers");
    expect(m.feed("x", 200)).toBeNull();
    expect(m.feed("Shift", 300)).toBeNull();
    expect(m.feed("?", 310)).toBe("help");
  });

  it("is case-sensitive (Shift+G is not g)", () => {
    const m = createKeySequenceMatcher(BINDINGS);
    expect(m.feed("G", 0)).toBeNull();
    expect(m.feed("D", 10)).toBeNull();
    expect(m.feed("C", 20)).toBeNull();
  });

  it("reset() clears a pending sequence", () => {
    const m = createKeySequenceMatcher(BINDINGS);
    m.feed("g", 0);
    m.reset();
    expect(m.feed("d", 10)).toBeNull();
  });
});

describe("isPaletteHotkey", () => {
  const base = { key: "k", ctrlKey: false, metaKey: false, altKey: false, shiftKey: false };
  it("accepts Ctrl+K and Cmd+K in either case", () => {
    expect(isPaletteHotkey({ ...base, ctrlKey: true })).toBe(true);
    expect(isPaletteHotkey({ ...base, metaKey: true })).toBe(true);
    expect(isPaletteHotkey({ ...base, key: "K", ctrlKey: true })).toBe(true);
  });
  it("rejects plain k and other chords", () => {
    expect(isPaletteHotkey(base)).toBe(false);
    expect(isPaletteHotkey({ ...base, ctrlKey: true, shiftKey: true })).toBe(false);
    expect(isPaletteHotkey({ ...base, ctrlKey: true, altKey: true })).toBe(false);
    expect(isPaletteHotkey({ ...base, key: "j", ctrlKey: true })).toBe(false);
  });
});

describe("typing and overlay detection", () => {
  function el(html: string, selector: string): HTMLElement {
    document.body.innerHTML = html;
    return document.querySelector(selector) as HTMLElement;
  }

  it("treats text fields, selects, contenteditable and the xterm console as typing targets", () => {
    expect(isTypingTarget(el('<input id="t">', "#t"))).toBe(true);
    expect(isTypingTarget(el('<input id="t" type="search">', "#t"))).toBe(true);
    expect(isTypingTarget(el('<textarea id="t"></textarea>', "#t"))).toBe(true);
    expect(isTypingTarget(el('<select id="t"><option>a</option></select>', "#t"))).toBe(true);
    expect(isTypingTarget(el('<div contenteditable="true"><span id="t">x</span></div>', "#t"))).toBe(true);
    expect(isTypingTarget(el('<div class="xterm"><textarea class="xterm-helper-textarea" id="t"></textarea></div>', "#t"))).toBe(true);
    expect(isTypingTarget(el('<div class="xterm"><div id="t"></div></div>', "#t"))).toBe(true);
    expect(isTypingTarget(el('<div role="combobox" id="t"></div>', "#t"))).toBe(true);
  });

  it("does not treat buttons, checkboxes, links or the page as typing targets", () => {
    expect(isTypingTarget(el('<button id="t">x</button>', "#t"))).toBe(false);
    expect(isTypingTarget(el('<input id="t" type="checkbox">', "#t"))).toBe(false);
    expect(isTypingTarget(el('<a id="t" href="/x">x</a>', "#t"))).toBe(false);
    expect(isTypingTarget(document.body)).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
    expect(isTypingTarget(window)).toBe(false);
  });

  it("refuses single-key shortcuts while typing, with modifiers, in menus or with a dialog open", () => {
    const ev = (target: EventTarget | null, extra: Partial<KeyboardEvent> = {}) => ({
      key: "c",
      ctrlKey: false,
      metaKey: false,
      altKey: false,
      shiftKey: false,
      repeat: false,
      isComposing: false,
      defaultPrevented: false,
      target,
      ...extra,
    });
    document.body.innerHTML = '<button id="b">x</button><input id="i">';
    const button = document.getElementById("b")!;
    const input = document.getElementById("i")!;
    expect(acceptsSingleKeyShortcut(ev(button))).toBe(true);
    expect(acceptsSingleKeyShortcut(ev(document.body))).toBe(true);
    expect(acceptsSingleKeyShortcut(ev(input))).toBe(false);
    expect(acceptsSingleKeyShortcut(ev(button, { ctrlKey: true }))).toBe(false);
    expect(acceptsSingleKeyShortcut(ev(button, { altKey: true }))).toBe(false);
    expect(acceptsSingleKeyShortcut(ev(button, { repeat: true }))).toBe(false);
    expect(acceptsSingleKeyShortcut(ev(button, { isComposing: true }))).toBe(false);
    expect(acceptsSingleKeyShortcut(ev(button, { defaultPrevented: true }))).toBe(false);
    // Shift is allowed: "?" is Shift+/ on many layouts
    expect(acceptsSingleKeyShortcut(ev(button, { key: "?", shiftKey: true }))).toBe(true);

    document.body.innerHTML = '<div role="menu" data-state="open"><div role="menuitem" id="m">Start</div></div>';
    expect(acceptsSingleKeyShortcut(ev(document.getElementById("m")))).toBe(false);

    document.body.innerHTML = '<button id="b">x</button><div role="alertdialog" data-state="open"></div>';
    expect(hasOpenOverlay(document)).toBe(true);
    expect(acceptsSingleKeyShortcut(ev(document.getElementById("b")), document)).toBe(false);

    document.body.innerHTML = '<button id="b">x</button><div role="dialog" data-state="closed"></div>';
    expect(hasOpenOverlay(document)).toBe(false);
    expect(acceptsSingleKeyShortcut(ev(document.getElementById("b")), document)).toBe(true);
  });
});

describe("isMacPlatform", () => {
  it("detects Apple platforms", () => {
    expect(isMacPlatform({ platform: "MacIntel", userAgent: "" })).toBe(true);
    expect(isMacPlatform({ platform: "", userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)" })).toBe(true);
    expect(isMacPlatform({ platform: "Win32", userAgent: "Mozilla/5.0 (Windows NT 10.0)" })).toBe(false);
    expect(isMacPlatform({ platform: "Linux x86_64", userAgent: "Mozilla/5.0 (X11; Linux x86_64)" })).toBe(false);
    expect(isMacPlatform(undefined)).toBe(false);
  });
});

describe("fuzzyScore", () => {
  it("ranks prefix > word-start substring > inner substring > subsequence", () => {
    const prefix = scoreToken("con", "console");
    const wordStart = scoreToken("test", "vanilla test");
    const inner = scoreToken("nil", "vanilla");
    const subseq = scoreToken("bkp", "backups");
    expect(prefix).toBe(1);
    expect(wordStart).toBeGreaterThan(inner);
    expect(inner).toBeGreaterThan(subseq);
    expect(subseq).toBeGreaterThan(0);
  });

  it("requires every token to match some keyword, in any order", () => {
    const kw = ["Console", "Vanilla Test", "terminal", "logs"];
    expect(fuzzyScore("vanilla console", kw)).toBeGreaterThan(0);
    expect(fuzzyScore("console vanilla", kw)).toBeGreaterThan(0);
    expect(fuzzyScore("vanilla backups", kw)).toBe(0);
  });

  it("is case and accent insensitive and treats an empty query as a match", () => {
    expect(fuzzyScore("CAFÉ", ["Cafe Server"])).toBe(1);
    expect(fuzzyScore("", ["anything"])).toBe(1);
    expect(fuzzyScore("x", [])).toBe(0);
  });

  it("prefers the label over secondary keywords", () => {
    const label = fuzzyScore("stop", ["Stop Vanilla Test", "Vanilla Test"]);
    const alias = fuzzyScore("stop", ["Shutdown", "stop"]);
    expect(label).toBeGreaterThan(alias);
  });

  it("rejects scattered letters that do not land on word starts", () => {
    expect(scoreToken("aei", "maintenance")).toBe(0);
    expect(scoreToken("cons", "account & password")).toBe(0);
    expect(scoreToken("cs", "create server")).toBeGreaterThan(0);
    expect(scoreToken("stng", "settings")).toBeGreaterThan(0);
  });
});

describe("orderGroups", () => {
  it("puts the group with the best match first and keeps ties in render order", () => {
    const groups = [
      { name: "nav", keywords: [["Dashboard"], ["Users & roles", "accounts"]] },
      { name: "servers", keywords: [["Vanilla Test"]] },
      { name: "pages", keywords: [["Console", "Vanilla Test"]] },
      { name: "prefs", keywords: [["Toggle theme"]] },
    ];
    expect(orderGroups(groups, "cons").map((g) => g.name)).toEqual(["pages", "nav", "servers", "prefs"]);
    expect(orderGroups(groups, "zzz").map((g) => g.name)).toEqual(["nav", "servers", "pages", "prefs"]);
  });
});

describe("palette storage", () => {
  function memoryStorage(initial: Record<string, string> = {}) {
    const data = new Map(Object.entries(initial));
    return {
      getItem: (k: string) => data.get(k) ?? null,
      setItem: (k: string, v: string) => void data.set(k, v),
      data,
    };
  }

  it("keeps the most recent ids first, de-duplicated and capped", () => {
    const s = memoryStorage();
    for (let i = 0; i < MAX_RECENT + 2; i++) pushRecent(`nav:/p${i}`, s);
    pushRecent("nav:/p3", s);
    const ids = readRecent(s);
    expect(ids).toHaveLength(MAX_RECENT);
    expect(ids[0]).toBe("nav:/p3");
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("survives corrupt data and throwing storage", () => {
    expect(readRecent(memoryStorage({ [RECENT_KEY]: "{not json" }))).toEqual([]);
    expect(readRecent(memoryStorage({ [RECENT_KEY]: '{"a":1}' }))).toEqual([]);
    expect(readRecent(memoryStorage({ [RECENT_KEY]: '["ok", 3, null, ""]' }))).toEqual(["ok"]);
    const throwing = {
      getItem: () => {
        throw new Error("SecurityError");
      },
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
    };
    expect(readRecent(throwing)).toEqual([]);
    expect(pushRecent("nav:/", throwing)).toEqual(["nav:/"]);
    expect(readSingleKeyShortcuts(throwing)).toBe(true);
    expect(() => writeSingleKeyShortcuts(false, throwing)).not.toThrow();
    expect(readRecent(undefined)).toEqual([]);
  });

  it("remembers whether single-key shortcuts are enabled", () => {
    const s = memoryStorage();
    expect(readSingleKeyShortcuts(s)).toBe(true);
    writeSingleKeyShortcuts(false, s);
    expect(readSingleKeyShortcuts(s)).toBe(false);
    writeSingleKeyShortcuts(true, s);
    expect(readSingleKeyShortcuts(s)).toBe(true);
  });
});
