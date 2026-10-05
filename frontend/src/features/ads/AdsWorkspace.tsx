import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useCallback, useEffect, useRef, useState } from "react"
import {
  AdsReportingService,
  type FrozenSelection,
  type ReportRow,
} from "@/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ManagementPreviewSheet } from "@/features/ad-management/ManagementPreviewSheet"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { AdsDetails } from "./AdsDetails"
import { AdsFilters } from "./AdsFilters"
import { AdsSummary } from "./AdsSummary"
import { AdsTable } from "./AdsTable"
import { BulkActionBar } from "./BulkActionBar"
import { ReportCoverageNotice } from "./ReportCoverageNotice"
import {
  type AdsSearch,
  childDimensionForColumn,
  DIMENSIONS,
  defaultAdsSearch,
  rowHierarchyId,
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
  const [queryRevision, setQueryRevision] = useState(0)
  const currentBcId = bc?.bc_id
  const [appliedBcId, setAppliedBcId] = useState<string | null>(null)
  const requestSearch =
    appliedBcId === currentBcId
      ? applied
      : { ...applied, page: 1, cursor: undefined, snapshot_id: undefined }
  const ads = useAdsQuery(requestSearch, queryRevision)
  const queryClient = useQueryClient()
  const items = ads.query.data?.items ?? []
  const [actionError, setActionError] = useState<string | null>(null)
  const [frozenSelection, setFrozenSelection] =
    useState<FrozenSelection | null>(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  const scopeRef = useRef(`${tenantId ?? ""}:${currentBcId ?? ""}`)
  const selectionRequestScopeRef = useRef<string | null>(null)
  const clearManagementPreview = useCallback(() => {
    selectionRequestScopeRef.current = null
    setFrozenSelection(null)
    setPreviewOpen(false)
  }, [])
  const syncMutation = useMutation({
    mutationFn: async () => {
      const refs = items.flatMap((row) => row.refs ?? [])
      const advertiserIds = [...new Set(refs.map((ref) => ref.advertiser_id))]
      // An empty first page is expected before the directory has its first
      // report.  The server resolves an empty list to every active account in
      // the selected BC, so the initial click can actually bootstrap history.
      return (
        await AdsReportingService.requestAdSync({
          path: { tenant_id: tenantId! },
          query: { bc_id: bc!.bc_id },
          body: {
            advertiser_ids: advertiserIds,
            scope: advertiserIds.length ? "report" : "history",
            ...(advertiserIds.length
              ? {
                  start_date: applied.start_date,
                  end_date: applied.end_date,
                }
              : {}),
            refs,
          },
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
            columns: ["name", "status", "spend", "d0_roas"],
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
    onSuccess: (selection) => {
      if (selectionRequestScopeRef.current !== scopeRef.current) return
      setFrozenSelection(selection)
      setPreviewOpen(true)
    },
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
    scopeRef.current = `${tenantId ?? ""}:${currentBcId}`
    clearManagementPreview()
    setAppliedBcId(currentBcId)
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
  }, [clearManagementPreview, currentBcId, tenantId])
  const update = (next: Partial<AdsSearch>) =>
    setSearch((current) => ({ ...current, ...next, page: next.page ?? 1 }))
  const apply = () => {
    ads.selection.clear()
    clearManagementPreview()
    setCursorHistory([])
    setSelectedRow(null)
    setQueryRevision((current) => current + 1)
    setApplied({
      ...search,
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    })
  }
  const reset = () => {
    const next = defaultAdsSearch()
    ads.selection.clear()
    clearManagementPreview()
    setCursorHistory([])
    setSelectedRow(null)
    setQueryRevision((current) => current + 1)
    setSearch(next)
    setApplied(next)
  }
  const navigateToChild = (row: ReportRow, columnKey: string) => {
    const dimension = childDimensionForColumn(columnKey)
    if (!dimension) {
      setSelectedRow(row)
      return
    }
    const parentId = rowHierarchyId(row, columnKey)
    const advertiserId =
      row.display?.account_id ?? row.refs?.[0]?.advertiser_id ?? null
    if (!parentId || !advertiserId) {
      setActionError("当前行缺少层级 ID，暂时无法跳转下级报表")
      return
    }
    const next: AdsSearch = {
      ...applied,
      dimension,
      advertiser_ids: [advertiserId],
      ids: [parentId],
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    }
    ads.selection.clear()
    clearManagementPreview()
    setSelectedRow(null)
    setCursorHistory([])
    setActionError(null)
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
            disabled={syncMutation.isPending || roleReadonly}
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
            disabled={!ads.snapshot || exportMutation.isPending || roleReadonly}
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
      <Card>
        <CardContent className="p-4">
          <AdsFilters
            search={search}
            onChange={update}
            onApply={apply}
            onReset={reset}
          />
        </CardContent>
      </Card>
      <ReportCoverageNotice coverage={ads.query.data?.coverage} />
      <AdsSummary summary={ads.query.data?.summary} />
      {ads.query.isFetching && (
        <p className="text-xs text-muted-foreground">正在加载当前报表…</p>
      )}
      <Card>
        <CardContent className="space-y-4 p-4">
          <Tabs
            value={applied.dimension}
            onValueChange={(value) => {
              const dimension = value as AdsSearch["dimension"]
              ads.selection.clear()
              clearManagementPreview()
              setCursorHistory([])
              setSelectedRow(null)
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
                  dimension={dimension.value}
                  selected={ads.selection.selected}
                  allMatching={ads.selection.allMatching}
                  total={ads.query.data?.total ?? 0}
                  onToggle={ads.selection.toggleRow}
                  onSelectAll={ads.selection.selectAllMatching}
                  onOpen={setSelectedRow}
                  onNavigate={navigateToChild}
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
      {ads.selection.selected.size > 0 && !roleReadonly && (
        <BulkActionBar
          selectedCount={ads.selection.selected.size}
          allMatching={ads.selection.allMatching}
          total={ads.query.data?.total ?? 0}
          disabled={selectionMutation.isPending || roleReadonly}
          onOpenPreview={() =>
            runAction(() => {
              selectionRequestScopeRef.current = scopeRef.current
              selectionMutation.mutate(undefined, {
                onError: (error) => setActionError(String(error)),
              })
            })
          }
        />
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
        snapshotId={ads.snapshot?.snapshot_id}
        onClose={() => setSelectedRow(null)}
      />
      {tenantId && bc?.bc_id && (
        <ManagementPreviewSheet
          tenantId={tenantId}
          bcId={bc.bc_id}
          selection={frozenSelection}
          open={previewOpen}
          onOpenChange={setPreviewOpen}
        />
      )}
    </div>
  )
}
