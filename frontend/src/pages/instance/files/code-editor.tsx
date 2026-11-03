/** Plain-textarea code editor with a line-number gutter (no editor dependency). */
import { useEffect, useMemo, useRef, type KeyboardEventHandler } from "react";
import { cn } from "@/lib/utils";
import { countLines } from "./file-utils";

const LINE_HEIGHT = 20; // px — matches leading-5 on both gutter and textarea

export interface CursorPos {
  line: number;
  col: number;
}

export function CodeEditor({
  id,
  value,
  onChange,
  readOnly,
  ariaLabel,
  jumpTo,
  onCursor,
  onKeyDown,
  invalid,
  className,
}: {
  id?: string;
  value: string;
  onChange?: (value: string) => void;
  readOnly?: boolean;
  ariaLabel?: string;
  /** Move the caret to a 1-based line once per distinct `key`. */
  jumpTo?: { line: number; key: string | number } | null;
  onCursor?: (pos: CursorPos) => void;
  onKeyDown?: KeyboardEventHandler<HTMLTextAreaElement>;
  invalid?: boolean;
  className?: string;
}) {
  const taRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);
  const jumped = useRef<string | number | null>(null);
  const lines = useMemo(() => countLines(value), [value]);
  const gutter = useMemo(() => Array.from({ length: lines }, (_, i) => i + 1).join("\n"), [lines]);
  const digits = String(lines).length;

  useEffect(() => {
    const ta = taRef.current;
    if (!jumpTo || !ta || jumped.current === jumpTo.key) return;
    jumped.current = jumpTo.key;
    let start = 0;
    for (let line = 1; line < jumpTo.line; line++) {
      const next = value.indexOf("\n", start);
      if (next === -1) break;
      start = next + 1;
    }
    const end = value.indexOf("\n", start);
    ta.focus();
    ta.setSelectionRange(start, end === -1 ? value.length : end);
    ta.scrollTop = Math.max(0, (jumpTo.line - 1) * LINE_HEIGHT - ta.clientHeight / 3);
    if (gutterRef.current) gutterRef.current.scrollTop = ta.scrollTop;
    onCursor?.({ line: Math.min(jumpTo.line, countLines(value)), col: 1 });
  }, [jumpTo, value, onCursor]);

  const reportCursor = () => {
    const ta = taRef.current;
    if (!ta || !onCursor) return;
    const pos = ta.selectionStart;
    const before = value.slice(0, pos);
    onCursor({ line: countLines(before), col: pos - (before.lastIndexOf("\n") + 1) + 1 });
  };

  return (
    <div
      className={cn(
        "flex min-h-0 overflow-hidden rounded-lg border bg-background transition-colors focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50",
        invalid && "border-destructive",
        className,
      )}
    >
      <div
        ref={gutterRef}
        aria-hidden
        className="shrink-0 overflow-hidden border-r bg-muted/40 text-right font-mono text-xs leading-5 text-muted-foreground select-none"
        style={{ width: `calc(${digits}ch + 1.5rem)` }}
      >
        <pre className="px-2 pt-2 pb-10 font-mono">{gutter}</pre>
      </div>
      <textarea
        ref={taRef}
        id={id}
        aria-label={ariaLabel}
        aria-invalid={invalid || undefined}
        value={value}
        readOnly={readOnly}
        onChange={(e) => onChange?.(e.target.value)}
        onScroll={(e) => {
          if (gutterRef.current) gutterRef.current.scrollTop = e.currentTarget.scrollTop;
        }}
        onSelect={reportCursor}
        onKeyDown={onKeyDown}
        wrap="off"
        spellCheck={false}
        autoCapitalize="off"
        autoComplete="off"
        autoCorrect="off"
        className="min-h-0 w-full flex-1 resize-none overflow-auto bg-transparent px-3 py-2 font-mono text-xs leading-5 whitespace-pre outline-none [tab-size:4] read-only:cursor-default"
      />
    </div>
  );
}
