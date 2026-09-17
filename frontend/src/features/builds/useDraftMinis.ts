import { useQuery } from "@tanstack/react-query"
import { BuildsService, type DraftSummary } from "@/client"
import { buildKey } from "./api"

export function useDraftMinis(
  tenantId: string,
  bcId: string,
  summary: DraftSummary | undefined,
  page = 1,
) {
  return useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId),
      summary?.draft_id,
      summary?.revision,
      summary?.status,
      "minis",
      page,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.minisOptions({
          path: { tenant_id: tenantId, draft_id: summary!.draft_id },
          query: { page },
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
