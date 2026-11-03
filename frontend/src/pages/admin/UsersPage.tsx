import { useQuery } from "@tanstack/react-query";
import { ShieldCheck, Users } from "lucide-react";
import { useSearchParams } from "react-router";
import { PageHeader } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuth } from "@/hooks/use-auth";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { PanelUser, PermissionDef, Role } from "@/lib/types";
import { AdminNoPermission } from "./admin-shared";
import { permissionDefsKey } from "./admin-utils";
import { RolesTab } from "./users/RolesTab";
import { UsersTab } from "./users/UsersTab";

export default function UsersPage() {
  const { can, user: me } = useAuth();
  const allowed = can("system.users");
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "roles" ? "roles" : "users";

  const users = useQuery({ queryKey: qk.users, queryFn: () => api.get<PanelUser[]>("/users"), enabled: allowed });
  const roles = useQuery({ queryKey: qk.roles, queryFn: () => api.get<Role[]>("/roles"), enabled: allowed });
  const defs = useQuery({
    queryKey: permissionDefsKey,
    queryFn: () => api.get<{ permissions: PermissionDef[] }>("/permissions"),
    enabled: allowed,
    staleTime: 5 * 60_000,
  });

  if (!me) return <LoadingState />;
  if (!allowed) return <AdminNoPermission permission="system.users" what="Managing users and roles" />;

  const usersReady = users.data && roles.data;
  const rolesReady = roles.data && defs.data;
  const usersError = users.error ?? roles.error;
  const rolesError = roles.error ?? defs.error;

  return (
    <div className="grid grid-cols-1 gap-4">
      <PageHeader
        className="pb-0"
        title="Users & roles"
        description="Panel accounts and what each one may do. Minecraft players are managed on each server's Players tab."
      />
      <Tabs
        value={tab}
        onValueChange={(v) => setParams(v === "roles" ? { tab: "roles" } : {}, { replace: true })}
        className="gap-4"
      >
        <TabsList>
          <TabsTrigger value="users" className="px-3">
            <Users /> Users
            {users.data && <span className="text-xs tabular-nums text-muted-foreground">{users.data.length}</span>}
          </TabsTrigger>
          <TabsTrigger value="roles" className="px-3">
            <ShieldCheck /> Roles & permissions
          </TabsTrigger>
        </TabsList>

        <TabsContent value="users">
          {usersReady ? (
            <UsersTab users={users.data} roles={roles.data} me={me} />
          ) : usersError ? (
            <ErrorState
              error={usersError}
              title="Couldn't load users"
              onRetry={() => {
                users.refetch();
                roles.refetch();
              }}
            />
          ) : (
            <LoadingState label="Loading users…" />
          )}
        </TabsContent>

        <TabsContent value="roles">
          {rolesReady ? (
            <RolesTab roles={roles.data} defs={defs.data.permissions} me={me} />
          ) : rolesError ? (
            <ErrorState
              error={rolesError}
              title="Couldn't load roles"
              onRetry={() => {
                roles.refetch();
                defs.refetch();
              }}
            />
          ) : (
            <LoadingState label="Loading roles…" />
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
