import { useEffect, useRef } from "react";
import {
  acceptsSingleKeyShortcut,
  createKeySequenceMatcher,
  hasOpenOverlay,
  isModifierKey,
  isPaletteHotkey,
  type ShortcutDef,
  type ShortcutId,
} from "./shortcuts";

interface Options {
  paletteOpen: boolean;
  onTogglePalette: () => void;
  singleKeyEnabled: boolean;
  /** Bindings the user may use (already filtered by permission). */
  bindings: readonly ShortcutDef[];
  onShortcut: (id: ShortcutId) => void;
}

/**
 * Window-level keyboard handling. Ctrl/Cmd+K is taken in the capture phase so it also works inside
 * inputs and the xterm console (which stops propagation of keys it handles); the single-key
 * shortcuts listen in the bubble phase, so any component that handles a key itself wins.
 */
export function useGlobalShortcuts({ paletteOpen, onTogglePalette, singleKeyEnabled, bindings, onShortcut }: Options) {
  const latest = useRef({ paletteOpen, onTogglePalette, singleKeyEnabled, onShortcut });
  useEffect(() => {
    latest.current = { paletteOpen, onTogglePalette, singleKeyEnabled, onShortcut };
  });

  useEffect(() => {
    const matcher = createKeySequenceMatcher(bindings);
    const onCapture = (e: KeyboardEvent) => {
      if (e.isComposing || !isPaletteHotkey(e)) return;
      // Another dialog (a confirmation, the shortcuts help) keeps the keyboard until it is closed.
      if (!latest.current.paletteOpen && hasOpenOverlay()) return;
      e.preventDefault();
      matcher.reset();
      latest.current.onTogglePalette();
    };
    const onBubble = (e: KeyboardEvent) => {
      if (!latest.current.singleKeyEnabled || isPaletteHotkey(e)) return;
      if (!acceptsSingleKeyShortcut(e)) {
        if (!isModifierKey(e.key)) matcher.reset();
        return;
      }
      const id = matcher.feed(e.key, e.timeStamp);
      if (id) {
        e.preventDefault();
        latest.current.onShortcut(id);
      }
    };
    window.addEventListener("keydown", onCapture, true);
    window.addEventListener("keydown", onBubble);
    return () => {
      window.removeEventListener("keydown", onCapture, true);
      window.removeEventListener("keydown", onBubble);
    };
  }, [bindings]);
}
