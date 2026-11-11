/**
 * A form over a game's field schema (lib/game-config.ts): config files on the Game settings tab, the
 * wizard's initial settings and options, the reset dialog's map settings. It edits form texts; the
 * caller turns them into values with diffTexts / valuesForCreate.
 */
import { Lock } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { groupFields, MASK } from "@/lib/game-config";
import type { ConfigField } from "@/lib/types";

function idFor(prefix: string, key: string): string {
  return `${prefix}-${key.replace(/[^A-Za-z0-9_-]/g, "_")}`;
}

export function ConfigFieldControl({
  field,
  text,
  onChange,
  disabled,
  error,
  idPrefix = "cfg",
  showKey = false,
}: {
  field: ConfigField;
  text: string;
  onChange: (text: string) => void;
  disabled?: boolean;
  error?: string | null;
  idPrefix?: string;
  showKey?: boolean;
}) {
  const id = idFor(idPrefix, field.key);
  const describedBy = [field.help ? `${id}-help` : "", error ? `${id}-error` : ""].filter(Boolean).join(" ") || undefined;
  const keyHint = showKey ? (
    <span className="truncate font-mono text-[10px] text-muted-foreground" title={field.key}>
      {field.key}
    </span>
  ) : null;
  const help = field.help ? (
    <p id={`${id}-help`} className="text-xs text-muted-foreground">
      {field.help}
    </p>
  ) : null;
  const err = error ? (
    <p id={`${id}-error`} className="text-xs text-destructive" role="alert">
      {error}
    </p>
  ) : null;

  if (field.type === "bool") {
    return (
      <div className="grid grid-cols-1 gap-1">
        <div className="flex items-start justify-between gap-4">
          <div className="grid min-w-0 gap-0.5">
            <Label htmlFor={id}>{field.label}</Label>
            {keyHint}
          </div>
          <Switch
            id={id}
            checked={text === "true"}
            onCheckedChange={(c) => onChange(c ? "true" : "false")}
            disabled={disabled}
            aria-describedby={describedBy}
          />
        </div>
        {help}
        {err}
      </div>
    );
  }

  let control;
  if (field.type === "enum") {
    control = (
      <Select value={text} onValueChange={onChange} disabled={disabled}>
        <SelectTrigger id={id} className="w-full" aria-describedby={describedBy} aria-invalid={!!error || undefined}>
          <SelectValue placeholder="Not set" />
        </SelectTrigger>
        <SelectContent>
          {(field.options ?? []).map((o) => (
            <SelectItem key={o} value={o}>
              {o}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  } else if (field.type === "list") {
    control = (
      <Textarea
        id={id}
        value={text}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        rows={3}
        placeholder="One entry per line"
        aria-describedby={describedBy}
        aria-invalid={!!error || undefined}
        spellCheck={false}
      />
    );
  } else {
    const numeric = field.type === "int" || field.type === "number";
    control = (
      <Input
        id={id}
        type={field.secret ? "password" : "text"}
        inputMode={numeric ? (field.type === "int" ? "numeric" : "decimal") : undefined}
        value={text}
        onChange={(e) => onChange(e.target.value)}
        onFocus={(e) => {
          // a masked secret is replaced, not edited
          if (field.secret && text === MASK) e.currentTarget.select();
        }}
        disabled={disabled}
        placeholder={field.nullable ? (numeric ? "Random" : "Not set") : undefined}
        autoComplete={field.secret ? "new-password" : "off"}
        aria-describedby={describedBy}
        aria-invalid={!!error || undefined}
        spellCheck={false}
      />
    );
  }
  return (
    <div className="grid grid-cols-1 content-start gap-1.5">
      <div className="grid min-w-0 gap-0.5">
        <Label htmlFor={id} className="flex items-center gap-1">
          {field.label}
          {field.secret && <Lock className="size-3 text-muted-foreground" aria-label="(secret)" />}
        </Label>
        {keyHint}
      </div>
      {control}
      {help}
      {field.secret && text === MASK && !error && (
        <p className="text-[11px] text-muted-foreground">Set. The panel never shows it; type a new value to replace it.</p>
      )}
      {(field.type === "int" || field.type === "number") && (field.min !== undefined || field.max !== undefined) && !error && (
        <p className="text-[11px] text-muted-foreground">
          {field.min !== undefined && field.max !== undefined
            ? `${field.min} – ${field.max}`
            : field.min !== undefined
              ? `Minimum ${field.min}`
              : `Maximum ${field.max}`}
        </p>
      )}
      {err}
    </div>
  );
}

export function ConfigFieldsForm({
  fields,
  texts,
  onChange,
  errors = {},
  disabled,
  idPrefix,
  showKeys,
}: {
  fields: readonly ConfigField[];
  texts: Record<string, string>;
  onChange: (key: string, text: string) => void;
  errors?: Record<string, string>;
  disabled?: boolean;
  idPrefix?: string;
  showKeys?: boolean;
}) {
  return (
    <div className="grid grid-cols-1 gap-5">
      {groupFields(fields).map(([group, list]) => (
        <fieldset key={group || "_"} className="grid grid-cols-1 gap-3">
          {group && <legend className="mb-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">{group}</legend>}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {list.map((f) => (
              <ConfigFieldControl
                key={f.key}
                field={f}
                text={texts[f.key] ?? ""}
                onChange={(t) => onChange(f.key, t)}
                disabled={disabled}
                error={errors[f.key]}
                idPrefix={idPrefix}
                showKey={showKeys}
              />
            ))}
          </div>
        </fieldset>
      ))}
    </div>
  );
}
