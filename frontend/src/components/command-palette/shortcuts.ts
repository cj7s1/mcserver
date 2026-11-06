/**
 * Keyboard shortcut logic for the command palette (pure, unit-tested in shortcuts.test.ts).
 *
 * Ctrl+K / Cmd+K toggles the palette everywhere (also inside inputs and the console). The single-key
 * shortcuts ("?", "g d", "g s", "c", …) only fire when focus is not in something the user types into
 * (inputs, textareas, selects, contenteditable, the xterm console) or inside an open dialog or menu,
 * and they can be switched off in the shortcuts dialog (WCAG 2.1.4 Character Key Shortcuts).
 */
import type { PermissionCode } from "@/lib/types";

export type ShortcutId = "help" | "go-dashboard" | "go-servers" | "go-modpacks" | "go-activity" | "create-server";

export interface ShortcutDef {
  id: ShortcutId;
  /** Key sequence as KeyboardEvent.key values, e.g. ["g", "d"]. */
  keys: readonly string[];
  description: string;
  /** Route the shortcut opens (also used to show the hint next to the matching palette entry). */
  to?: string;
  /** Global permission the shortcut needs; it is hidden and inert without it. */
  perm?: PermissionCode;
}

export const SEQUENCE_TIMEOUT_MS = 1000;

export const SHORTCUTS: readonly ShortcutDef[] = [
  { id: "help", keys: ["?"], description: "Show keyboard shortcuts" },
  { id: "go-dashboard", keys: ["g", "d"], description: "Go to the dashboard", to: "/" },
  { id: "go-servers", keys: ["g", "s"], description: "Go to servers", to: "/servers" },
  { id: "go-modpacks", keys: ["g", "m"], description: "Go to modpacks", to: "/modpacks" },
  { id: "go-activity", keys: ["g", "a"], description: "Go to activity", to: "/activity" },
  { id: "create-server", keys: ["c"], description: "Create a server", to: "/servers/new", perm: "instance.create" },
];

/** Keys that never start, continue or break a sequence (pressing Shift to type "?" must not reset it). */
const MODIFIER_KEYS = new Set(["Shift", "Control", "Alt", "AltGraph", "Meta", "CapsLock", "Fn", "FnLock", "Hyper", "Super", "OS"]);

export function isModifierKey(key: string): boolean {
  return MODIFIER_KEYS.has(key);
}

export interface KeySequenceMatcher<T extends string> {
  /** Feed one keydown; returns the id of a completed binding, or null. */
  feed(key: string, timeStamp: number): T | null;
  reset(): void;
  /** True while a started sequence (e.g. "g") waits for its next key. */
  readonly pending: boolean;
}

/**
 * Matches multi-key sequences such as "g d". A sequence must be completed within `timeoutMs` per key;
 * a key that does not continue the started sequence drops it (it is not re-interpreted on its own, so
 * "g" followed by "c" does not open the create wizard by accident).
 */
export function createKeySequenceMatcher<T extends string>(
  bindings: readonly { id: T; keys: readonly string[] }[],
  timeoutMs = SEQUENCE_TIMEOUT_MS,
): KeySequenceMatcher<T> {
  let buffer: string[] = [];
  let last = Number.NEGATIVE_INFINITY;

  const isPrefix = (seq: readonly string[]) =>
    bindings.some((b) => b.keys.length > seq.length && seq.every((k, i) => b.keys[i] === k));
  const exact = (seq: readonly string[]) =>
    bindings.find((b) => b.keys.length === seq.length && seq.every((k, i) => b.keys[i] === k));

  return {
    feed(key, timeStamp) {
      if (!key || isModifierKey(key)) return null;
      if (buffer.length && timeStamp - last > timeoutMs) buffer = [];
      last = timeStamp;
      const seq = [...buffer, key];
      const hit = exact(seq);
      if (hit) {
        buffer = [];
        return hit.id;
      }
      // Keep a valid prefix; anything else (including a key that breaks a started sequence) is dropped.
      buffer = isPrefix(seq) ? seq : [];
      return null;
    },
    reset() {
      buffer = [];
    },
    get pending() {
      return buffer.length > 0;
    },
  };
}

type KeyLike = Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey">;

/** Ctrl+K or Cmd+K (no Alt/Shift), whatever the keyboard layout reports in upper or lower case. */
export function isPaletteHotkey(e: KeyLike): boolean {
  return (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k";
}

const NON_TEXT_INPUT_TYPES = new Set(["button", "checkbox", "color", "file", "image", "radio", "range", "reset", "submit"]);
const TEXT_ROLES = new Set(["textbox", "searchbox", "combobox", "spinbutton"]);

/** True when keystrokes on `target` are text entry (inputs, textareas, selects, contenteditable, xterm). */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!target || typeof (target as Element).closest !== "function") return false;
  const el = target as HTMLElement;
  if (el.closest(".xterm")) return true;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") return !NON_TEXT_INPUT_TYPES.has(((el as HTMLInputElement).type || "text").toLowerCase());
  if (el.isContentEditable || el.closest('[contenteditable=""], [contenteditable="true"], [contenteditable="plaintext-only"]')) return true;
  const role = el.getAttribute("role");
  return !!role && TEXT_ROLES.has(role);
}

const OVERLAY_SELECTOR = '[role="dialog"], [role="alertdialog"], [role="menu"], [role="menubar"], [role="listbox"]';
const OPEN_OVERLAY_SELECTOR =
  '[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"], [role="menu"][data-state="open"], [role="listbox"][data-state="open"]';

/** True when focus is inside a dialog/menu/listbox (letters there do typeahead or belong to the dialog). */
export function isInsideOverlay(target: EventTarget | null): boolean {
  if (!target || typeof (target as Element).closest !== "function") return false;
  return !!(target as Element).closest(OVERLAY_SELECTOR);
}

/** True when any Radix dialog, alert dialog, menu or listbox is open in `doc`. */
export function hasOpenOverlay(doc: Pick<Document, "querySelector"> | undefined = globalThis.document): boolean {
  return !!doc?.querySelector(OPEN_OVERLAY_SELECTOR);
}

type ShortcutEventLike = KeyLike & Pick<KeyboardEvent, "target" | "repeat" | "isComposing" | "defaultPrevented">;

/** Whether a keydown may be interpreted as a single-key shortcut at all. */
export function acceptsSingleKeyShortcut(e: ShortcutEventLike, doc?: Pick<Document, "querySelector">): boolean {
  if (e.defaultPrevented || e.isComposing || e.repeat) return false;
  if (e.ctrlKey || e.metaKey || e.altKey) return false;
  if (isTypingTarget(e.target) || isInsideOverlay(e.target)) return false;
  return !hasOpenOverlay(doc);
}

export function isMacPlatform(nav: Pick<Navigator, "userAgent"> & { platform?: string } | undefined = globalThis.navigator): boolean {
  if (!nav) return false;
  return /Mac|iPhone|iPad|iPod/i.test(nav.platform || nav.userAgent || "");
}

/** Human-readable key caps for display: Ctrl+K becomes ["⌘", "K"] on macOS and ["Ctrl", "K"] elsewhere. */
export function paletteHotkeyCaps(mac: boolean): string[] {
  return mac ? ["⌘", "K"] : ["Ctrl", "K"];
}

/** aria-keyshortcuts value for the palette trigger. */
export const PALETTE_ARIA_KEYSHORTCUTS = "Control+K Meta+K";

export function shortcutFor(to: string): ShortcutDef | undefined {
  return SHORTCUTS.find((s) => s.to === to);
}
