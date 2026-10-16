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
    // 后端在 PREPARING 时只返回空目录；等摘要 READY 后再核对，避免每两秒发无效请求。
    enabled:
      !!bcId && !!draft && draft.bc_id === bcId && draft.status === "READY",
    // 准备页和定向卡片共享同一目录；新修订/重新准备仍用新键读取。
    staleTime: 15_000,
    queryFn: async ({ signal }) =>
      (
        await BuildsService.targetingOptions({
          path: { tenant_id: tenantId, draft_id: draft!.draft_id },
          signal,
        })
      ).data,
  })
}
