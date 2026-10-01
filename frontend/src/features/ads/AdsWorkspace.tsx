import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
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
import {
  type AdsSearch,
  DIMENSIONS,
  defaultAdsSearch,
  toReportingFilter,
} from "./search"
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
  const items = ads.query.data?.items ?? []
  const currentBcId = bc?.bc_id
  const [actionError, setActionError] = useState<string | null>(null)
  const syncMutation = useMutation({
    mutationFn: async () => {
      const refs = items.flatMap((row) => row.refs ?? [])
      const advertiserIds = [...new Set(refs.map((ref) => ref.advertiser_id))]
      if (!advertiserIds.length) throw new Error("当前筛选没有可刷新的广告账户")
      return (
        await AdsReportingService.requestAdSync({
          path: { tenant_id: tenantId! },
          query: { bc_id: bc!.bc_id },
          body: { advertiser_ids: advertiserIds, scope: "report", refs },
        })
      ).data
    },
  })
  const exportMutation = useMutation({
    mutationFn: async () => {
      if (!ads.snapshot) throw new Error("当前报表没有可导出的快照")
      return (
        await AdsReportingService.createReportExport({
          path: { tenant_id: tenantId! },
          query: { bc_id: bc!.bc_id },
          body: {
            snapshot_id: ads.snapshot.snapshot_id,
            idempotency_key: `ads-export-${ads.snapshot.snapshot_id}-${Date.now()}`,
          },
        })
      ).data
    },
  })
  const viewMutation = useMutation({
    mutationFn: async () =>
      (
        await AdsReportingService.createReportView({
          path: { tenant_id: tenantId! },
          query: { bc_id: bc!.bc_id },
          body: {
            name: "广告报表当前筛选",
            filters: toReportingFilter(applied),
            columns: [
              "name",
              "drama",
              "status",
              "spend",
              "d0_roas",
              "target_roas",
            ],
          },
        })
      ).data,
  })
  const selectionMutation = useMutation({
    mutationFn: async () => {
      if (!ads.snapshot) throw new Error("当前报表没有可操作的快照")
      return (
        await AdsReportingService.freezeAdSelection({
          path: { tenant_id: tenantId! },
          query: { bc_id: bc!.bc_id },
          body: {
            snapshot_id: ads.snapshot.snapshot_id,
            mode: ads.selection.allMatching ? "ALL_MATCHING" : "EXPLICIT",
            row_keys: [...ads.selection.selected],
            excluded_row_keys: [...ads.selection.excluded],
          },
        })
      ).data
    },
  })
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
    if (!currentBcId) return
    setCursorHistory([])
    setSelectedRow(null)
    setSearch((current) => ({
      ...current,
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    }))
    setApplied((current) => ({
      ...current,
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    }))
  }, [currentBcId])
  const update = (next: Partial<AdsSearch>) =>
    setSearch((current) => ({ ...current, ...next, page: next.page ?? 1 }))
  const apply = () => {
    setCursorHistory([])
    setApplied({
      ...search,
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    })
  }
  const reset = () => {
    const next = defaultAdsSearch()
    setCursorHistory([])
    setSearch(next)
    setApplied(next)
  }
  const runAction = (action: () => void) => {
    setActionError(null)
    action()
  }
  const roleReadonly = scope?.role === "viewer"
  const nextPage = () => {
    const nextCursor = ads.query.data?.next_cursor
    if (!nextCursor) return
    setCursorHistory((history) => [...history, applied.cursor ?? null])
    setApplied((current) => ({
      ...current,
      cursor: nextCursor,
      snapshot_id: ads.snapshot?.snapshot_id,
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
          <Button
            variant="outline"
            disabled={syncMutation.isPending}
            onClick={() =>
              runAction(() =>
                syncMutation.mutate(undefined, {
                  onSuccess: () =>
                    void queryClient.invalidateQueries({
                      queryKey: ["tenant", tenantId, "ads", bc?.bc_id],
                    }),
                  onError: (error) => setActionError(String(error)),
                }),
              )
            }
          >
            {syncMutation.isPending ? "刷新中…" : "刷新"}
          </Button>
          <Button
            variant="outline"
            disabled={!ads.snapshot || exportMutation.isPending}
            onClick={() =>
              runAction(() =>
                exportMutation.mutate(undefined, {
                  onError: (error) => setActionError(String(error)),
                }),
              )
            }
          >
            {exportMutation.isPending ? "导出中…" : "导出"}
          </Button>
          <Button
            variant="outline"
            disabled={viewMutation.isPending || roleReadonly}
            onClick={() =>
              runAction(() =>
                viewMutation.mutate(undefined, {
                  onError: (error) => setActionError(String(error)),
                }),
              )
            }
          >
            {viewMutation.isPending ? "保存中…" : "保存筛选"}
          </Button>
        </div>
      </div>
      {latest && (
        <p className="text-xs text-muted-foreground">
          最新同步：{String(latest)}
        </p>
      )}
      <ReportCoverageNotice coverage={ads.query.data?.coverage} />
      <AdsSummary summary={ads.query.data?.summary} rows={items} />
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
              setCursorHistory([])
              setApplied((current) => ({
                ...current,
                dimension,
                page: 1,
                cursor: undefined,
                snapshot_id: undefined,
              }))
              setSearch((current) => ({
                ...current,
                dimension,
                page: 1,
                cursor: undefined,
                snapshot_id: undefined,
              }))
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
          <Button
            disabled={selectionMutation.isPending || roleReadonly}
            onClick={() =>
              runAction(() =>
                selectionMutation.mutate(undefined, {
                  onError: (error) => setActionError(String(error)),
                }),
              )
            }
          >
            {selectionMutation.isPending ? "处理中…" : "批量操作"}
          </Button>
        </div>
      )}
      {actionError && (
        <p role="alert" className="text-sm text-destructive">
          操作失败：{actionError}
        </p>
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
