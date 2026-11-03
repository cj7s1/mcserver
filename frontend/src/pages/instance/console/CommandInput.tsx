import { CornerDownLeft, Loader2 } from "lucide-react";
import { useId, useRef, useState, type KeyboardEvent } from "react";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput, InputGroupText } from "@/components/ui/input-group";

const MAX_HISTORY = 100;
const MAX_COMMAND = 1400; // backend limit (apps/console/consumers.py)

function storageKey(instanceId: string): string {
  return `mcpanel.console.history.${instanceId}`;
}

function loadHistory(instanceId: string): string[] {
  try {
    const v: unknown = JSON.parse(sessionStorage.getItem(storageKey(instanceId)) ?? "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string").slice(-MAX_HISTORY) : [];
  } catch {
    return [];
  }
}

function saveHistory(instanceId: string, history: string[]) {
  try {
    sessionStorage.setItem(storageKey(instanceId), JSON.stringify(history.slice(-MAX_HISTORY)));
  } catch {
    /* storage unavailable (private mode / quota) — history just won't persist */
  }
}

/**
 * Console command line with shell-style history (↑/↓), persisted per instance in
 * sessionStorage so it survives tab switches and reloads but not the browser session.
 */
export function CommandInput({
  instanceId,
  disabledReason,
  pending,
  onSubmit,
}: {
  instanceId: string;
  disabledReason: string | null;
  pending: number;
  onSubmit: (command: string) => boolean;
}) {
  const [value, setValue] = useState("");
  const [history, setHistory] = useState<string[]>(() => loadHistory(instanceId));
  const [index, setIndex] = useState(-1); // -1 = editing a new line
  const draft = useRef("");
  const id = useId();
  const disabled = !!disabledReason;

  const submit = () => {
    const cmd = value.trim().replace(/^\/+/, "");
    if (!cmd || disabled) return;
    if (!onSubmit(cmd)) return;
    const next = history[history.length - 1] === cmd ? history : [...history, cmd].slice(-MAX_HISTORY);
    setHistory(next);
    saveHistory(instanceId, next);
    setValue("");
    setIndex(-1);
    draft.current = "";
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowUp") {
      if (!history.length) return;
      e.preventDefault();
      if (index === -1) draft.current = value;
      const next = Math.min(index + 1, history.length - 1);
      setIndex(next);
      setValue(history[history.length - 1 - next]);
    } else if (e.key === "ArrowDown") {
      if (index === -1) return;
      e.preventDefault();
      const next = index - 1;
      setIndex(next);
      setValue(next === -1 ? draft.current : history[history.length - 1 - next]);
    } else if (e.key === "Escape" && value) {
      e.preventDefault();
      setValue("");
      setIndex(-1);
    }
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      className="grid gap-1.5"
    >
      <label htmlFor={id} className="sr-only">
        Console command
      </label>
      <InputGroup className="h-9 font-mono">
        <InputGroupAddon>
          <InputGroupText className="font-mono text-primary">&gt;</InputGroupText>
        </InputGroupAddon>
        <InputGroupInput
          id={id}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            if (index !== -1) setIndex(-1);
          }}
          onKeyDown={onKeyDown}
          disabled={disabled}
          maxLength={MAX_COMMAND}
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          placeholder={disabled ? "Commands unavailable" : "Type a command, e.g. list or say Hello"}
          aria-describedby={`${id}-help`}
          className="font-mono text-sm"
        />
        <InputGroupAddon align="inline-end">
          {pending > 0 && <Loader2 className="size-3.5 animate-spin text-muted-foreground" aria-label={`${pending} command(s) awaiting a result`} />}
          <InputGroupButton type="submit" size="xs" variant="secondary" disabled={disabled || !value.trim()}>
            <CornerDownLeft /> Send
          </InputGroupButton>
        </InputGroupAddon>
      </InputGroup>
      <p id={`${id}-help`} className="text-xs text-muted-foreground">
        {disabledReason ?? (
          <>
            Enter to send · <kbd className="font-mono">↑</kbd>/<kbd className="font-mono">↓</kbd> history · no leading slash needed ·
            commands run over RCON and are recorded in the audit log
          </>
        )}
      </p>
    </form>
  );
}
