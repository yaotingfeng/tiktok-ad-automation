import { useQuery } from "@tanstack/react-query"
import { BuildsService, type DraftSummary } from "@/client"
import { buildKey } from "@/features/builds/api"

export function useTargetingDirectory(
  tenantId: string,
  bcId?: string,
  draft?: DraftSummary,
) {
  return useQuery({
    queryKey: draft
      ? [
          ...buildKey(tenantId, bcId || ""),
          draft.draft_id,
          "targeting",
          draft.revision,
          draft.status,
        ]
      : ["tenant", tenantId, "targeting-regions", bcId],
    enabled: !!bcId && (!draft || draft.bc_id === bcId),
    queryFn: async ({ signal }) =>
      (
        await (draft
          ? BuildsService.targetingOptions({
              path: { tenant_id: tenantId, draft_id: draft.draft_id },
              signal,
            })
          : BuildsService.targetingRegions({
              path: { tenant_id: tenantId },
              query: { bc_id: bcId! },
              signal,
            }))
      ).data,
    refetchInterval: draft?.status === "PREPARING" ? 2000 : false,
  })
}
