import { useMutation, useQuery } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { ChevronDown, ChevronUp } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import {
  AdsReportingService,
  type FrozenSelection,
  type ReportRow,
} from "@/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  type ManagementAction,
  ManagementPreviewSheet,
} from "@/features/ad-management/ManagementPreviewSheet"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import useAuth from "@/hooks/useAuth"
import { AdsDetails } from "./AdsDetails"
import { AdsFilters } from "./AdsFilters"
import { AdsSummary } from "./AdsSummary"
import { AdsTable } from "./AdsTable"
import { AdsTableToolbar } from "./AdsTableToolbar"
import { AdsTrendChart } from "./AdsTrendChart"
import {
  applyColumnPrefs,
  type ColumnPrefs,
  loadColumnPrefs,
  saveColumnPrefs,
} from "./columnPrefs"
import { columnsFor, useMetricCatalog } from "./metricCatalog"
import { ReportStatusBar } from "./ReportStatusBar"
import {
  type AdsSearch,
  childDimension,
  DIMENSIONS,
  defaultAdsSearch,
  type ParentLink,
  rowObjectId,
  toReportingFilter,
} from "./search"
import { useAdsQuery } from "./useAdsQuery"
import { useAdsTrend } from "./useAdsTrend"
export function AdsWorkspace() {
  const { tenantId, bc, scope, bcDirectory } = useTenantScope()
  const { user } = useAuth()
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
  const items = ads.query.data?.items ?? []
  const catalog = useMetricCatalog()
  const [actionError, setActionError] = useState<string | null>(null)
  const [frozenSelection, setFrozenSelection] =
    useState<FrozenSelection | null>(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [managementAction, setManagementAction] =
    useState<ManagementAction | null>(null)
  const scopeRef = useRef(`${tenantId ?? ""}:${currentBcId ?? ""}`)
  const selectionRequestScopeRef = useRef<string | null>(null)

  // 列偏好状态：仅在 tenantId 和 user.id 都就绪后读写
  const prefsReady = Boolean(tenantId && user?.id)
  const prefsScope = prefsReady ? `${tenantId}:${user!.id}` : null
  const [columnPrefs, setColumnPrefs] = useState<ColumnPrefs | null>(null)

  // 当 scope 就绪时加载偏好
  useEffect(() => {
    if (prefsScope) {
      setColumnPrefs(loadColumnPrefs(prefsScope, applied.dimension))
    }
  }, [prefsScope, applied.dimension])

  const handlePrefsChange = (prefs: ColumnPrefs) => {
    setColumnPrefs(prefs)
    if (prefsScope) {
      saveColumnPrefs(prefsScope, applied.dimension, prefs)
    }
  }
  const allColumns = columnsFor(catalog.data ?? [], applied.dimension)
  const visibleColumns = applyColumnPrefs(
    allColumns,
    prefsReady ? columnPrefs : null,
  )

  // 汇总卡片与趋势状态
  const [activeMetric, setActiveMetric] = useState("spend")
  const [trendOpen, setTrendOpen] = useState(() => {
    try {
      return localStorage.getItem("ads-trend-open") === "true"
    } catch {
      return false
    }
  })
  const saveTrendOpen = (open: boolean) => {
    setTrendOpen(open)
    try {
      localStorage.setItem("ads-trend-open", String(open))
    } catch {
      // 忽略存储失败
    }
  }
  const granularity =
    applied.start_date === applied.end_date ? "hour" : ("day" as const)
  const trend = useAdsTrend({
    level: "account",
    advertiserIds: applied.advertiser_ids,
    startDate: applied.start_date,
    endDate: applied.end_date,
    granularity,
    enabled: trendOpen,
  })
  const activeColumn = catalog.data?.find((d) => d.key === activeMetric)

  const clearManagementPreview = useCallback(() => {
    selectionRequestScopeRef.current = null
    setFrozenSelection(null)
    setPreviewOpen(false)
    setManagementAction(null)
  }, [])
  const [syncNotice, setSyncNotice] = useState<string | null>(null)
  const syncStatus = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "ads-sync-status",
      currentBcId,
      applied.start_date,
      applied.end_date,
    ],
    enabled: Boolean(tenantId && currentBcId),
    queryFn: async () =>
      (
        await AdsReportingService.getAdSyncStatus({
          path: { tenant_id: tenantId! },
          query: {
            bc_id: currentBcId!,
            start_date: applied.start_date,
            end_date: applied.end_date,
          },
        })
      ).data,
    refetchInterval: 30_000,
  })
  const publicationRef = useRef<{ scope: string; version: string } | null>(null)
  const publicationScope = `${tenantId}:${currentBcId}:${applied.start_date}:${applied.end_date}`
  const clearSelection = ads.selection.clear
  const pendingRefreshScope = useRef<string | null>(null)
  const [newDataAvailable, setNewDataAvailable] = useState(false)
  const loadLatest = useCallback(() => {
    pendingRefreshScope.current = null
    setNewDataAvailable(false)
    clearSelection()
    clearManagementPreview()
    setSelectedRow(null)
    setCursorHistory([])
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
    setQueryRevision((current) => current + 1)
    setSyncNotice("报表数据已更新")
  }, [clearSelection, clearManagementPreview])
  useEffect(() => {
    if (!syncStatus.data) return
    const version = syncStatus.data.publication_version
    const previous = publicationRef.current
    publicationRef.current = { scope: publicationScope, version }
    if (!previous || previous.scope !== publicationScope) {
      pendingRefreshScope.current = null
      setNewDataAvailable(false)
      return
    }
    if (previous.version === version) return
    // 主动刷新完成后换快照；普通后台发布不能打断跨页选择和已打开的预览。
    if (pendingRefreshScope.current === publicationScope) loadLatest()
    else setNewDataAvailable(true)
  }, [syncStatus.data, publicationScope, loadLatest])
  const syncMutation = useMutation({
    mutationFn: async () => {
      // 手动刷新按当前筛选账户插队，不从当前页推导范围，也不触发历史回补。
      const requestScope = scopeRef.current
      const result = (
        await AdsReportingService.requestAdSyncRefresh({
          path: { tenant_id: tenantId! },
          query: { bc_id: bc!.bc_id },
          body: { advertiser_ids: applied.advertiser_ids ?? [] },
        })
      ).data
      return { result, requestScope }
    },
    onSuccess: ({ result, requestScope }) => {
      if (scopeRef.current !== requestScope) return
      if (result.status === "QUEUED")
        pendingRefreshScope.current = publicationScope
      setSyncNotice(
        result.status === "SYNC_DISABLED"
          ? "报表同步未开启"
          : result.status === "API_BINDING_REQUIRED"
            ? "该 BC 未接入 API，无法同步报表"
            : "已加入刷新队列",
      )
      void syncStatus.refetch()
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
            columns: ["name", "status", ...visibleColumns.map((c) => c.key)],
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
    setSyncNotice(null)
    setActionError(null)
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
  const sort = (sortBy: string) => {
    // 同一列再次点击切换方向；新列中名称默认升序，数值指标默认降序（大的在前）。
    const isName = sortBy.endsWith("_name") || sortBy === "name"
    const direction =
      applied.sort_by === sortBy
        ? applied.sort_direction === "asc"
          ? "desc"
          : "asc"
        : isName
          ? "asc"
          : "desc"
    const next = {
      ...applied,
      sort_by: sortBy,
      sort_direction: direction as "asc" | "desc",
      page: 1,
      cursor: undefined,
      snapshot_id: undefined,
    }
    ads.selection.clear()
    clearManagementPreview()
    setCursorHistory([])
    setSelectedRow(null)
    setQueryRevision((current) => current + 1)
    setSearch(next)
    setApplied(next)
  }
  // 切换到某个层级并按父级 ID 筛选；下钻和点击上级路径共用同一套状态重置。
  const filterTo = (
    dimension: AdsSearch["dimension"],
    advertiserId: string,
    parentId: string,
  ) => {
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
  const navigateToChild = (row: ReportRow) => {
    const dimension = childDimension(applied.dimension)
    if (!dimension) {
      setSelectedRow(row)
      return
    }
    const parentId = rowObjectId(row, applied.dimension)
    const advertiserId =
      row.display?.account_id ??
      row.display?.advertiser_id ??
      row.refs?.[0]?.advertiser_id ??
      null
    if (!parentId || !advertiserId) {
      setActionError("当前行缺少层级 ID，暂时无法跳转下级报表")
      return
    }
    filterTo(dimension, advertiserId, parentId)
  }
  const filterByParent = (link: ParentLink, advertiserId: string) => {
    if (!advertiserId) {
      setActionError("当前行缺少账户 ID，暂时无法按上级筛选")
      return
    }
    // 点了哪个上级，就看它的直接下级：账户 → 系列，系列 → 广告组，广告组 → 广告。
    const dimension =
      link.kind === "account"
        ? "campaign"
        : link.kind === "campaign"
          ? "adgroup"
          : "ad"
    filterTo(dimension, advertiserId, link.id)
  }
  const runAction = (action: () => void) => {
    setActionError(null)
    action()
  }
  const roleReadonly = scope?.role === "viewer"
  // 工具栏动作：先记下动作与发起时的 BC 范围，再冻结选择；冻结成功后才打开预览。
  const startManagementAction = (next: ManagementAction) => {
    if (roleReadonly) return
    runAction(() => {
      setManagementAction(next)
      selectionRequestScopeRef.current = scopeRef.current
      selectionMutation.mutate(undefined, {
        onError: (error) => setActionError(String(error)),
      })
    })
  }
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
        </div>
      </div>
      {newDataAvailable && (
        <Button variant="outline" size="sm" onClick={loadLatest}>
          查看最新数据
        </Button>
      )}
      <ReportStatusBar
        status={syncStatus.data}
        coverage={ads.query.data?.coverage}
        notice={syncNotice}
      />
      <Card>
        <CardContent className="p-4">
          <AdsFilters
            search={search}
            onChange={update}
            onApply={apply}
            onReset={reset}
            onSaveView={() =>
              runAction(() =>
                viewMutation.mutate(undefined, {
                  onError: (error) => setActionError(String(error)),
                }),
              )
            }
            savingView={viewMutation.isPending}
            disableSaveView={roleReadonly}
          />
        </CardContent>
      </Card>
      {catalog.isError && (
        <div className="flex items-center gap-2 text-sm text-destructive">
          <p>指标定义读取失败</p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void catalog.refetch()}
          >
            重试
          </Button>
        </div>
      )}
      <AdsSummary
        summary={ads.query.data?.summary}
        catalog={catalog.data ?? []}
        activeMetric={activeMetric}
        onSelectMetric={setActiveMetric}
      />
      <div className="space-y-3">
        <Button
          variant="outline"
          size="sm"
          onClick={() => saveTrendOpen(!trendOpen)}
          className="w-full"
        >
          {trendOpen ? (
            <>
              <ChevronUp className="mr-2 h-4 w-4" />
              收起趋势
            </>
          ) : (
            <>
              <ChevronDown className="mr-2 h-4 w-4" />
              展开趋势
            </>
          )}
        </Button>
        {trendOpen && activeColumn && (
          <AdsTrendChart
            trend={trend.data}
            metric={{
              key: activeColumn.key,
              label: activeColumn.basic_label,
              fullName: activeColumn.basic_name_zh,
              unit: activeColumn.unit,
              recoveryDay: null,
            }}
            loading={trend.isLoading}
            error={trend.isError}
            onRetry={() => void trend.refetch()}
          />
        )}
      </div>
      {ads.query.isLoading && (
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
              // 加载新维度的列偏好
              if (prefsScope) {
                setColumnPrefs(loadColumnPrefs(prefsScope, dimension))
              }
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
                className="mt-4 space-y-3"
              >
                <AdsTableToolbar
                  columns={allColumns}
                  prefs={columnPrefs}
                  onPrefsChange={handlePrefsChange}
                  selection={{
                    selectedCount: ads.selection.selected.size,
                    allMatching: ads.selection.allMatching,
                    total: ads.query.data?.total ?? 0,
                    dimension: dimension.value,
                    readonly: roleReadonly,
                    busy: selectionMutation.isPending || !ads.snapshot,
                    onAction: startManagementAction,
                    onSelectAllMatching: ads.selection.selectAllMatching,
                    onClear: ads.selection.clear,
                  }}
                />
                <AdsTable
                  rows={items}
                  dimension={dimension.value}
                  columns={visibleColumns}
                  endDate={applied.end_date}
                  selected={ads.selection.selected}
                  allMatching={ads.selection.allMatching}
                  total={ads.query.data?.total ?? 0}
                  onToggle={ads.selection.toggleRow}
                  onToggleAllOnPage={(checked) =>
                    ads.selection.toggleRows(items, checked)
                  }
                  onOpen={setSelectedRow}
                  onDrill={navigateToChild}
                  onFilterParent={filterByParent}
                  nextCursor={ads.query.data?.next_cursor}
                  hasPrevious={cursorHistory.length > 0}
                  onNext={nextPage}
                  onPrevious={previousPage}
                  sortBy={applied.sort_by ?? "spend"}
                  sortDirection={applied.sort_direction ?? "desc"}
                  onSort={sort}
                />
              </TabsContent>
            ))}
          </Tabs>
        </CardContent>
      </Card>
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
          action={managementAction}
          open={previewOpen}
          onOpenChange={setPreviewOpen}
        />
      )}
    </div>
  )
}
