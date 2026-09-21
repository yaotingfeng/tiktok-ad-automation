import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { BuildsService, type DraftSummary } from "@/client"
import { buildKey } from "./api"

export function useDraftMinis(
  tenantId: string,
  bcId: string,
  summary: DraftSummary | undefined,
  page = 1,
  query = "",
) {
  return useQuery({
    queryKey: [
      ...buildKey(tenantId, bcId),
      summary?.draft_id,
      summary?.revision,
      summary?.status,
      "minis",
      page,
      query,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.minisOptions({
          path: { tenant_id: tenantId, draft_id: summary!.draft_id },
          query: { page, query: query || undefined },
          signal,
        })
      ).data,
    enabled:
      summary?.bc_id === bcId &&
      summary.account_count > 0 &&
      summary.drama_count > 0,
    refetchInterval: summary?.status === "PREPARING" ? 2000 : false,
    // 搜索/翻页保留目录，草稿修订或状态变化则必须重新读取，避免展示旧选择。
    placeholderData: (previous, previousQuery) =>
      previousQuery &&
      previousQuery.queryKey[5] === summary?.revision &&
      previousQuery.queryKey[6] === summary?.status
        ? keepPreviousData(previous)
        : undefined,
  })
}
