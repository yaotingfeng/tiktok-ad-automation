import { useQuery } from "@tanstack/react-query"
import { BuildsService, type DraftSummary } from "@/client"
import { buildKey } from "@/features/builds/api"

// 搭建目录只查当前草稿；草稿加载前不再查询历史 BC 场景目录。
export function useTargetingDirectory(
  tenantId: string,
  bcId?: string,
  draft?: DraftSummary,
) {
  return useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId || ""),
      draft?.draft_id,
      "targeting",
      draft?.revision,
      draft?.status,
    ],
    enabled: !!bcId && !!draft && draft.bc_id === bcId,
    queryFn: async ({ signal }) =>
      (
        await BuildsService.targetingOptions({
          path: { tenant_id: tenantId, draft_id: draft!.draft_id },
          signal,
        })
      ).data,
    refetchInterval: draft?.status === "PREPARING" ? 2000 : false,
  })
}
