import { useQuery } from "@tanstack/react-query"
import { AdsReportingService } from "@/client"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { type AdsDimension, MAX_LINKED_IDS } from "../search"

/** 详情里跳转到的关联对象：剧的关联系列、层级路径里的账户/系列/广告组。 */
export type LinkedTarget = {
  dimension: AdsDimension
  id: string
  advertiserId: string
  name: string
}

/**
 * 按 ID 读取关联对象在当前日期范围内的真实报表行。
 *
 * 与表格同一个查询接口：返回的行带快照，详情里的启停、改预算等操作据此冻结选择，
 * 和在该维度报表里点开的详情完全一致。带 include_empty，零消耗的对象也能打开。
 * 查询键第 4 段为 "linked"、第 5 段为 BC：操作提交后的回读随详情一起刷新。
 */
export function useLinkedRows({
  dimension,
  ids,
  advertiserId,
  startDate,
  endDate,
  enabled = true,
}: {
  dimension: AdsDimension
  ids: string[]
  advertiserId?: string
  startDate: string
  endDate: string
  enabled?: boolean
}) {
  const { tenantId, bc } = useTenantScope()
  const bcId = bc?.bc_id
  const scopedIds = ids.slice(0, MAX_LINKED_IDS)
  return useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "ads",
      "linked",
      bcId,
      dimension,
      advertiserId ?? null,
      scopedIds,
      startDate,
      endDate,
    ],
    enabled: Boolean(enabled && tenantId && bcId && scopedIds.length),
    staleTime: 15_000,
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.queryAds({
          path: { tenant_id: tenantId! },
          query: {
            bc_id: bcId!,
            dimension,
            start_date: startDate,
            end_date: endDate,
            ids: scopedIds.join(","),
            advertiser_id: advertiserId,
            include_empty: true,
            limit: 100,
            sort_by: "spend",
            sort_direction: "desc",
          },
          signal,
        })
      ).data,
  })
}
