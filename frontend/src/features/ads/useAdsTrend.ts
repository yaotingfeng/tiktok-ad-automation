import { useQuery } from "@tanstack/react-query"
import { AdsReportingService, type TrendPublic } from "@/client"
import { useTenantScope } from "@/features/tenants/TenantScope"

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
  return useQuery({
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
