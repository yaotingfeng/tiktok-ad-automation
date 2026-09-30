import { useQuery } from "@tanstack/react-query"
import { useEffect, useMemo, useState } from "react"
import {
  type AdsQueryPage,
  AdsReportingService,
  type ReportRow,
} from "@/client"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { type AdsSearch, toAdsQuery } from "./search"
export function useAdsQuery(search: AdsSearch) {
  const { tenantId, bc, scope } = useTenantScope()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [allMatching, setAllMatching] = useState(false)
  const [excluded, setExcluded] = useState<Set<string>>(new Set())
  const bcId = bc?.bc_id ?? null
  const queryKey = useMemo(
    () => ["tenant", tenantId, "ads", bcId, search] as const,
    [tenantId, bcId, search],
  )
  const query = useQuery<AdsQueryPage>({
    queryKey,
    enabled: !!tenantId && !!bcId,
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.queryAds({
          path: { tenant_id: tenantId! },
          query: { bc_id: bcId!, ...toAdsQuery(search) },
          signal,
        })
      ).data,
    placeholderData: (previous) => previous,
  })
  // BC 变化必须清空跨页冻结选择，避免不同 BC 的 row_key 复用。
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset is intentionally scoped to tenant and BC changes.
  useEffect(() => {
    setSelected(new Set())
    setExcluded(new Set())
    setAllMatching(false)
  }, [tenantId, bcId])
  const toggleRow = (row: ReportRow, checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(row.row_key)
      else next.delete(row.row_key)
      return next
    })
    if (allMatching)
      setExcluded((current) => {
        const next = new Set(current)
        if (checked) next.delete(row.row_key)
        else next.add(row.row_key)
        return next
      })
  }
  const selectAllMatching = () => {
    setAllMatching(true)
    setSelected(new Set(query.data?.items?.map((row) => row.row_key) ?? []))
    setExcluded(new Set())
  }
  const clear = () => {
    setSelected(new Set())
    setExcluded(new Set())
    setAllMatching(false)
  }
  return {
    query,
    snapshot: query.data?.snapshot ?? null,
    selection: {
      selected,
      excluded,
      allMatching,
      toggleRow,
      selectAllMatching,
      clear,
    },
    role: scope?.role,
  }
}
