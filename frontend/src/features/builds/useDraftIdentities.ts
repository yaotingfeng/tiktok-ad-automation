import { useQuery } from "@tanstack/react-query"
import { BuildsService, type DraftSummary } from "@/client"
import { buildKey } from "./api"

export function useDraftIdentities(
  tenantId: string,
  bcId: string,
  summary: DraftSummary | undefined,
) {
  return useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId),
      summary?.draft_id,
      summary?.revision,
      summary?.status,
      "identities",
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.identityOptions({
          path: { tenant_id: tenantId, draft_id: summary!.draft_id },
          signal,
        })
      ).data,
    enabled:
      summary?.bc_id === bcId &&
      summary.account_count > 0 &&
      summary.drama_count > 0,
    refetchInterval: summary?.status === "PREPARING" ? 2000 : false,
  })
}
