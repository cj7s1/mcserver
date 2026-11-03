/** Name, description, library group and icon (PATCH /instances/{id}). */
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { ServerIcon } from "@/components/ServerIcon";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { errorMessage } from "@/lib/api";
import { useInstances } from "@/lib/queries";
import type { Instance } from "@/lib/types";
import { Field, fieldErrorsOf, ReadOnlyNotice, SaveBar, usePatchInstance, useReportDirty, type TabProps } from "./settings-ui";

type Fields = Pick<Instance, "name" | "description" | "group" | "icon_url">;
const KEYS: (keyof Fields)[] = ["name", "description", "group", "icon_url"];

function validate(f: Fields): Partial<Record<keyof Fields, string>> {
  const e: Partial<Record<keyof Fields, string>> = {};
  if (!f.name.trim()) e.name = "A name is required";
  else if (f.name.length > 80) e.name = "At most 80 characters";
  if (f.description.length > 2000) e.description = "At most 2000 characters";
  if (f.group.length > 60) e.group = "At most 60 characters";
  if (f.icon_url) {
    if (f.icon_url.length > 500) e.icon_url = "At most 500 characters";
    else {
      try {
        const u = new URL(f.icon_url);
        if (!["http:", "https:"].includes(u.protocol)) e.icon_url = "Use an http(s) URL";
      } catch {
        e.icon_url = "Enter a full URL, e.g. https://example.com/icon.png";
      }
    }
  }
  return e;
}

export function GeneralTab({ instance, can, onDirtyChange }: TabProps) {
  const canEdit = can("instance.configure");
  const patch = usePatchInstance(instance.id);
  const all = useInstances("all");
  const [draft, setDraft] = useState<Partial<Fields>>({});
  const current: Fields = {
    name: draft.name ?? instance.name,
    description: draft.description ?? instance.description,
    group: draft.group ?? instance.group,
    icon_url: draft.icon_url ?? instance.icon_url,
  };
  const changed = KEYS.filter((k) => draft[k] !== undefined && draft[k] !== instance[k]);
  const errors = validate(current);
  const serverErrors = fieldErrorsOf(patch.error);
  const err = (k: keyof Fields) => errors[k] ?? serverErrors[k] ?? null;
  const disabled = !canEdit || patch.isPending;
  useReportDirty(changed.length > 0, onDirtyChange);

  const groups = useMemo(
    () => [...new Set((all.data ?? []).map((i) => i.group).filter(Boolean))].sort((a, b) => a.localeCompare(b)),
    [all.data],
  );
  const set = (k: keyof Fields, v: string) => {
    setDraft((d) => ({ ...d, [k]: v }));
    if (patch.error) patch.reset();
  };

  return (
    <div className="grid grid-cols-1 gap-4">
      {!canEdit && <ReadOnlyNotice />}
      <Section title="General" description="How this server appears in the panel. These changes apply immediately and need no restart.">
        <div className="grid gap-5 lg:grid-cols-[1fr_auto]">
          <div className="grid grid-cols-1 gap-4">
            <Field id="general-name" label="Name" error={err("name")}>
              <Input
                id="general-name"
                value={current.name}
                onChange={(e) => set("name", e.target.value)}
                maxLength={80}
                disabled={disabled}
                aria-invalid={!!err("name") || undefined}
              />
            </Field>
            <Field
              id="general-description"
              label="Description"
              error={err("description")}
              aside={<span className="text-[11px] text-muted-foreground tabular-nums">{current.description.length}/2000</span>}
            >
              <Textarea
                id="general-description"
                value={current.description}
                onChange={(e) => set("description", e.target.value)}
                rows={3}
                maxLength={2000}
                disabled={disabled}
                className="min-h-20"
                aria-invalid={!!err("description") || undefined}
              />
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field id="general-group" label="Group" error={err("group")} hint="Servers with the same group are listed together.">
                <Input
                  id="general-group"
                  value={current.group}
                  onChange={(e) => set("group", e.target.value)}
                  maxLength={60}
                  list="general-group-options"
                  placeholder="e.g. Survival"
                  disabled={disabled}
                  aria-invalid={!!err("group") || undefined}
                  autoComplete="off"
                />
                <datalist id="general-group-options">
                  {groups.map((g) => (
                    <option key={g} value={g} />
                  ))}
                </datalist>
              </Field>
              <Field id="general-icon" label="Icon URL" error={err("icon_url")} hint="Square image, shown in lists. Leave empty for initials.">
                <Input
                  id="general-icon"
                  type="url"
                  inputMode="url"
                  value={current.icon_url}
                  onChange={(e) => set("icon_url", e.target.value.trim())}
                  placeholder="https://…"
                  disabled={disabled}
                  aria-invalid={!!err("icon_url") || undefined}
                  autoComplete="off"
                />
              </Field>
            </div>
          </div>
          <div className="flex items-center gap-3 lg:flex-col lg:items-center lg:pt-6">
            <ServerIcon
              key={current.icon_url}
              url={errors.icon_url ? null : current.icon_url}
              name={current.name || instance.name}
              type={instance.server_type}
              className="size-20 text-xl"
            />
            <span className="text-xs text-muted-foreground">Preview</span>
          </div>
        </div>
      </Section>
      {canEdit && (
        <SaveBar
          count={changed.length}
          saving={patch.isPending}
          disabled={Object.keys(errors).length > 0}
          onDiscard={() => {
            setDraft({});
            patch.reset();
          }}
          onSave={() => {
            const body: Partial<Instance> = {};
            for (const k of changed) body[k] = current[k].trim();
            patch.mutate(body, {
              onSuccess: () => {
                setDraft({});
                toast.success("Saved");
              },
              onError: (e) => toast.error(errorMessage(e)),
            });
          }}
        />
      )}
    </div>
  );
}
