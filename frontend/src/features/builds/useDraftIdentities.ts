import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { BuildsService, type DraftSummary } from "@/client"
import { buildKey } from "./api"

export function useDraftIdentities(
  tenantId: string,
  bcId: string,
  summary: DraftSummary | undefined,
  query = "",
) {
  return useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId),
      summary?.draft_id,
      summary?.revision,
      summary?.status,
      "identities",
      query,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.identityOptions({
          path: { tenant_id: tenantId, draft_id: summary!.draft_id },
          query: { query: query || undefined },
          signal,
        })
      ).data,
    enabled:
      summary?.bc_id === bcId &&
      summary.account_count > 0 &&
      summary.drama_count > 0,
    refetchInterval: summary?.status === "PREPARING" ? 2000 : false,
    // 只在同一草稿版本内保留搜索结果；身份保存后的新版本不能沿用旧目录状态。
    placeholderData: (previous, previousQuery) =>
      previousQuery &&
      previousQuery.queryKey[5] === summary?.revision &&
      previousQuery.queryKey[6] === summary?.status
        ? keepPreviousData(previous)
        : undefined,
  })
}
