import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useEffect, useState } from "react"
import { AdsReportingService, type ReportRow } from "@/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { AdsDetails } from "./AdsDetails"
import { AdsFilters } from "./AdsFilters"
import { AdsSummary } from "./AdsSummary"
import { AdsTable } from "./AdsTable"
import { AdsTrend } from "./AdsTrend"
import { ReportCoverageNotice } from "./ReportCoverageNotice"
import { type AdsSearch, DIMENSIONS, defaultAdsSearch } from "./search"
import { useAdsQuery } from "./useAdsQuery"
export function AdsWorkspace() {
  const { tenantId, bc, scope, bcDirectory } = useTenantScope()
  const navigate = useNavigate()
  const [search, setSearch] = useState<AdsSearch>(() => defaultAdsSearch())
  const [selectedRow, setSelectedRow] = useState<ReportRow | null>(null)
  const [applied, setApplied] = useState<AdsSearch>(search)
  const [cursorHistory, setCursorHistory] = useState<Array<string | null>>([])
  const ads = useAdsQuery(applied)
  const queryClient = useQueryClient()
  const trend = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "ads",
      bc?.bc_id,
      "trend",
      ads.snapshot?.snapshot_id,
      applied,
    ],
    enabled: !!tenantId && !!bc && !!ads.snapshot,
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.reportTrend({
          path: { tenant_id: tenantId! },
          query: {
            bc_id: bc!.bc_id,
            dimension: applied.dimension,
            start_date: applied.start_date,
            end_date: applied.end_date,
            query: applied.query || undefined,
            snapshot_id: ads.snapshot!.snapshot_id,
          },
          signal,
        })
      ).data,
  })
  useEffect(() => {
    if (!bc && tenantId && bcDirectory?.items[0]?.bc_id) {
      void navigate({
        to: "/tenants/$tenantId/ads",
        params: { tenantId },
        search: { bc_id: bcDirectory.items[0].bc_id },
        replace: true,
      })
    }
  }, [bc, bcDirectory, navigate, tenantId])
  useEffect(() => {
    setSearch((current) => ({ ...current, page: 1 }))
    setApplied((current) => ({ ...current, page: 1 }))
  }, [bc?.bc_id])
  const update = (next: Partial<AdsSearch>) =>
    setSearch((current) => ({ ...current, ...next, page: next.page ?? 1 }))
  const apply = () => {
    setCursorHistory([])
    setApplied({ ...search, page: 1, cursor: undefined })
  }
  const reset = () => {
    const next = defaultAdsSearch()
    setCursorHistory([])
    setSearch(next)
    setApplied(next)
  }
  const items = ads.query.data?.items ?? []
  const refresh = () => {
    void queryClient.invalidateQueries({
      queryKey: ["tenant", tenantId, "ads", bc?.bc_id],
    })
  }
  const roleReadonly = scope?.role === "viewer"
  const nextPage = () => {
    const nextCursor = ads.query.data?.next_cursor
    if (!nextCursor) return
    setCursorHistory((history) => [...history, applied.cursor ?? null])
    setApplied((current) => ({
      ...current,
      cursor: nextCursor,
      page: current.page + 1,
    }))
  }
  const previousPage = () => {
    setCursorHistory((history) => {
      const next = [...history]
      const cursor = next.pop() ?? null
      setApplied((current) => ({
        ...current,
        cursor: cursor ?? undefined,
        page: Math.max(1, current.page - 1),
      }))
      return next
    })
  }
  const latest = (ads.query.data?.coverage?.latest_sync_at ??
    ads.query.data?.coverage?.observed_at) as string | undefined
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <WorkspacePageTitle>广告报表</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            {bc?.name ?? "当前 BC"} · 六维广告效果与配置
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={refresh}>
            刷新
          </Button>
          <Button variant="outline" disabled={!ads.snapshot}>
            导出
          </Button>
          <Button variant="outline" disabled={roleReadonly}>
            保存筛选
          </Button>
        </div>
      </div>
      {latest && (
        <p className="text-xs text-muted-foreground">
          最新同步：{String(latest)}
        </p>
      )}
      <ReportCoverageNotice coverage={ads.query.data?.coverage} />
      <AdsSummary summary={ads.query.data?.summary} />
      <AdsTrend trend={trend.data} />
      <Card>
        <CardContent className="space-y-4 p-4">
          <AdsFilters
            search={search}
            onChange={update}
            onApply={apply}
            onReset={reset}
          />
          <Tabs
            value={applied.dimension}
            onValueChange={(value) => {
              const dimension = value as AdsSearch["dimension"]
              setApplied((current) => ({ ...current, dimension, page: 1 }))
              setSearch((current) => ({ ...current, dimension, page: 1 }))
            }}
          >
            <TabsList>
              {DIMENSIONS.map((dimension) => (
                <TabsTrigger key={dimension.value} value={dimension.value}>
                  {dimension.label}
                </TabsTrigger>
              ))}
            </TabsList>
            {DIMENSIONS.map((dimension) => (
              <TabsContent
                key={dimension.value}
                value={dimension.value}
                className="mt-4"
              >
                <AdsTable
                  rows={items}
                  selected={ads.selection.selected}
                  allMatching={ads.selection.allMatching}
                  total={ads.query.data?.total ?? 0}
                  onToggle={ads.selection.toggleRow}
                  onSelectAll={ads.selection.selectAllMatching}
                  onOpen={setSelectedRow}
                  nextCursor={ads.query.data?.next_cursor}
                  hasPrevious={cursorHistory.length > 0}
                  onNext={nextPage}
                  onPrevious={previousPage}
                />
              </TabsContent>
            ))}
          </Tabs>
        </CardContent>
      </Card>
      {ads.selection.selected.size > 0 && (
        <div className="sticky bottom-4 z-10 flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-background p-3 shadow-lg">
          <span>
            已选{" "}
            {ads.selection.allMatching
              ? `全部 ${ads.query.data?.total ?? 0} 条`
              : ads.selection.selected.size}{" "}
            条
          </span>
          <Button disabled={roleReadonly}>批量操作</Button>
        </div>
      )}
      {ads.query.isError && (
        <p role="alert" className="text-sm text-destructive">
          广告报表读取失败，请刷新重试。
        </p>
      )}
      <AdsDetails
        row={selectedRow}
        ref={selectedRow?.refs?.[0] ?? null}
        onClose={() => setSelectedRow(null)}
      />
    </div>
  )
}
