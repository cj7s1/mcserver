import { useId } from "react";
import { Section } from "@/components/PageHeader";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { ServerType } from "@/lib/types";
import { useServerTypes } from "../../modpacks/modpackQueries";
import { describedBy, Field } from "../WizardBits";
import { CATEGORY_TYPES, type Derived, type FieldErrors, TYPE_LABELS, type WizardState } from "../wizardModel";

const GROUPS: { label: string; types: ServerType[] }[] = [
  { label: "Vanilla", types: CATEGORY_TYPES.vanilla },
  { label: "Plugin servers", types: CATEGORY_TYPES.plugins },
  { label: "Mod loaders", types: CATEGORY_TYPES.mods },
];

/** Import mode: which software the archived server ran (the version picker follows below). */
export function ImportSoftware({
  patch,
  d,
  errors,
}: {
  patch: (p: Partial<WizardState>) => void;
  d: Derived;
  errors: FieldErrors;
}) {
  const id = useId();
  const serverTypes = useServerTypes();
  const hint =
    "Use the software and Minecraft version the files were made with. The panel installs exactly this; it does not read it from the archive.";
  return (
    <Section title="Server software">
      <Field id={id} label="Software of the imported server" hint={hint} error={errors.serverType} className="max-w-md">
        <Select
          value={d.serverType ?? "VANILLA"}
          onValueChange={(v) => patch({ serverType: v as ServerType, minecraftVersion: null, loaderVersion: null })}
        >
          <SelectTrigger
            id={id}
            className="w-full"
            aria-invalid={!!errors.serverType || undefined}
            aria-describedby={describedBy(id, errors.serverType, hint)}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            {GROUPS.map((g) => (
              <SelectGroup key={g.label}>
                <SelectLabel>{g.label}</SelectLabel>
                {g.types.map((t) => (
                  <SelectItem key={t} value={t}>
                    {serverTypes.data?.find((s) => s.id === t)?.label ?? TYPE_LABELS[t]}
                  </SelectItem>
                ))}
              </SelectGroup>
            ))}
          </SelectContent>
        </Select>
      </Field>
    </Section>
  );
}
