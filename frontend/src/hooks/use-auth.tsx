import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { qk, useAuthState } from "@/lib/queries";
import type { Me, PermissionCode } from "@/lib/types";

export function useAuth() {
  const { data, isLoading, error, refetch } = useAuthState();
  const qc = useQueryClient();
  const user: Me | null = data?.user ?? null;
  return {
    state: data,
    user,
    isLoading,
    error,
    refetch,
    can: (code: PermissionCode) => !!user?.permissions.includes(code),
    logout: async () => {
      await api.post("/auth/logout");
      qc.clear();
      await qc.invalidateQueries({ queryKey: qk.auth });
    },
  };
}
