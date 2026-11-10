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
import { variantLabel } from "@/lib/games";
import { CATEGORY_TYPES, type Derived, type FieldErrors, type WizardState } from "../wizardModel";

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
    "Choose the software and Minecraft version the files were made with. Nothing is preselected: the panel installs exactly this and does not read it from the archive, and a newer version upgrades the world on its first start (it cannot be downgraded).";
  return (
    <Section title="Server software">
      <Field id={id} label="Software of the imported server" hint={hint} error={errors.serverType} className="max-w-md">
        <Select
          value={d.serverType ?? ""}
          onValueChange={(v) => patch({ serverType: v as ServerType, minecraftVersion: null, loaderVersion: null, javaOverride: "" })}
        >
          <SelectTrigger
            id={id}
            className="w-full"
            aria-invalid={!!errors.serverType || undefined}
            aria-describedby={describedBy(id, errors.serverType, hint)}
          >
            <SelectValue placeholder="Choose the software" />
          </SelectTrigger>
          <SelectContent position="popper">
            {GROUPS.map((g) => (
              <SelectGroup key={g.label}>
                <SelectLabel>{g.label}</SelectLabel>
                {g.types.map((t) => (
                  <SelectItem key={t} value={t}>
                    {serverTypes.data?.find((s) => s.id === t)?.label ?? variantLabel(t)}
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
