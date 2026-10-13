import { useQuery } from "@tanstack/react-query"
import { AdsReportingService, type TrendPublic } from "@/client"
import { useTenantScope } from "@/features/tenants/TenantScope"

const HOUR_SYNC_POLL_MS = 3_000

export function useAdsTrend(params: {
  level: TrendPublic["level"]
  advertiserIds?: string[]
  ids?: string[]
  startDate: string
  endDate: string
  granularity: "day" | "hour"
  enabled: boolean
}) {
  const { tenantId, bc } = useTenantScope()
  const bcId = bc?.bc_id
  return useQuery<TrendPublic | undefined>({
    // 键包含 BC：切换 BC 后旧请求的结果不会显示到新 BC。
    // queryKey 必须稳定：用基本类型而非对象引用
    queryKey: [
      "tenant",
      tenantId,
      "ads-trend",
      bcId,
      params.level,
      params.advertiserIds?.join(",") ?? "",
      params.ids?.join(",") ?? "",
      params.startDate,
      params.endDate,
      params.granularity,
    ],
    enabled: params.enabled && Boolean(tenantId && bcId),
    staleTime: 60_000,
    // 小时数据按需拉取：后端排队拉取期间返回 syncing，轮询到数据就绪为止。
    refetchInterval: (query) =>
      query.state.data?.syncing ? HOUR_SYNC_POLL_MS : false,
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.adTrend({
          path: { tenant_id: tenantId! },
          query: {
            bc_id: bcId!,
            level: params.level,
            advertiser_ids: params.advertiserIds?.join(",") || undefined,
            ids: params.ids?.join(",") || undefined,
            start_date: params.startDate,
            end_date: params.endDate,
            granularity: params.granularity,
          },
          signal,
        })
      ).data,
  })
}
