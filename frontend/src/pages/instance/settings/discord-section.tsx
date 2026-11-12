/**
 * Settings → General → Discord (B3): whether the Discord bot offers this server, which Discord roles may start
 * and stop it (instead of the default roles), and whether `/join` shows its join password. Saved on its own.
 * Answers of an older panel have none of the keys: nothing is shown.
 */
import { Bot, Loader2, Save } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/hooks/use-auth";
import { errorMessage } from "@/lib/api";
import type { Instance } from "@/lib/types";
import { idsError, parseIds, useDiscordSettings } from "../../admin/system/discord-model";
import { fieldErrorsOf, FieldError, Notice, usePatchInstance } from "./settings-ui";

export function DiscordServerSection({ instance, canEdit }: { instance: Instance; canEdit: boolean }) {
  const { can } = useAuth();
  const admin = can("system.settings");
  // the Discord server's roles by name: only an administrator may read the bot's settings
  const discord = useDiscordSettings(admin && instance.discord_available !== undefined);
  const guildRoles = discord.data?.status.guild_roles ?? [];
  const patch = usePatchInstance(instance.id);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [sharePassword, setSharePassword] = useState<boolean | null>(null);
  const [roles, setRoles] = useState<string[] | null>(null);
  const [rolesText, setRolesText] = useState<string | null>(null);
  if (instance.discord_available === undefined) return null;

  const savedRoles = instance.discord_roles ?? [];
  const cur = {
    available: available ?? !!instance.discord_available,
    sharePassword: sharePassword ?? !!instance.discord_share_password,
    roles: guildRoles.length ? (roles ?? savedRoles) : rolesText !== null ? parseIds(rolesText) : savedRoles,
  };
  const rolesErr = idsError(cur.roles);
  const server = fieldErrorsOf(patch.error);
  const body: Partial<Instance> = {};
  if (cur.available !== !!instance.discord_available) body.discord_available = cur.available;
  if (cur.sharePassword !== !!instance.discord_share_password) body.discord_share_password = cur.sharePassword;
  if (JSON.stringify([...cur.roles].sort()) !== JSON.stringify([...savedRoles].sort())) body.discord_roles = cur.roles;
  const dirty = Object.keys(body).length > 0;
  const disabled = !canEdit || patch.isPending;
  const reset = () => {
    setAvailable(null);
    setSharePassword(null);
    setRoles(null);
    setRolesText(null);
  };

  return (
    <Section
      title={
        <span className="flex items-center gap-2">
          <Bot className="size-4 text-muted-foreground" aria-hidden /> Discord
        </span>
      }
      description={
        <>
          What friends can do with this server in your Discord server.
          {admin ? (
            <>
              {" "}
              The bot itself is set up under{" "}
              <Link to="/admin/system" className="font-medium text-primary hover:underline">
                System → Discord
              </Link>
              .
            </>
          ) : null}
        </>
      }
    >
      <div className="grid gap-4">
        <div className="flex items-start justify-between gap-4">
          <div className="grid gap-0.5">
            <Label htmlFor="discord-available">Available in Discord</Label>
            <p className="text-xs text-muted-foreground">
              Everyone in your Discord server sees it with /servers and /status and gets the join text with /join; members with
              the allowed roles start and stop it.
            </p>
          </div>
          <Switch id="discord-available" checked={cur.available} onCheckedChange={setAvailable} disabled={disabled} />
        </div>

        <fieldset className="grid gap-1.5" disabled={disabled}>
          <legend className="text-sm font-medium">Roles that may start and stop it (optional)</legend>
          {guildRoles.length ? (
            <div className="grid gap-1.5 sm:grid-cols-2">
              {guildRoles.map((r) => {
                const id = `server-discord-role-${r.id}`;
                return (
                  <div key={r.id} className="flex items-center gap-2">
                    <Checkbox
                      id={id}
                      checked={cur.roles.includes(r.id)}
                      onCheckedChange={(v) => setRoles(v === true ? [...cur.roles, r.id] : cur.roles.filter((x) => x !== r.id))}
                    />
                    <Label htmlFor={id} className="font-normal">
                      {r.name}
                    </Label>
                  </div>
                );
              })}
            </div>
          ) : (
            <Input
              aria-label="Discord role ids for this server"
              value={rolesText ?? savedRoles.join(", ")}
              placeholder="Role ids, separated by commas"
              onChange={(e) => setRolesText(e.target.value)}
              aria-invalid={!!(rolesErr || server.discord_roles) || undefined}
              className="font-mono"
            />
          )}
          <p className="text-xs text-muted-foreground">Empty: the default roles of the Discord settings decide.</p>
          <FieldError message={rolesErr ?? server.discord_roles} />
        </fieldset>

        <div className="flex items-start justify-between gap-4">
          <div className="grid gap-0.5">
            <Label htmlFor="discord-share-password">Let /join show the password</Label>
            <p className="text-xs text-muted-foreground">
              Then everyone in your Discord server can read the join password. Off: /join says to ask you for it.
            </p>
          </div>
          <Switch id="discord-share-password" checked={cur.sharePassword} onCheckedChange={setSharePassword} disabled={disabled} />
        </div>
        {cur.sharePassword && !instance.discord_share_password && (
          <Notice tone="warning">Everyone who is in your Discord server will be able to read this server's join password.</Notice>
        )}

        {canEdit && (
          <div className="flex justify-end gap-2">
            {dirty && (
              <Button size="sm" variant="outline" onClick={reset} disabled={patch.isPending}>
                Discard
              </Button>
            )}
            <Button
              size="sm"
              disabled={!dirty || !!rolesErr || patch.isPending}
              onClick={() =>
                patch.mutate(body, {
                  onSuccess: () => {
                    reset();
                    toast.success("Discord settings of this server saved");
                  },
                  onError: (err) => {
                    if (!fieldErrorsOf(err).discord_roles) toast.error(errorMessage(err));
                  },
                })
              }
            >
              {patch.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save
            </Button>
          </div>
        )}
      </div>
    </Section>
  );
}
