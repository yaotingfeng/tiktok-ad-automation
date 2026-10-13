import { useQuery } from "@tanstack/react-query"
import type { ReactNode } from "react"
import { AdsReportingService, type ReportRow } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import type { ManagementAction } from "@/features/ad-management/ManagementPreviewSheet"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { rowDisplayName } from "../AdsNameCell"
import {
  actionsFor,
  toolbarAction,
  toolbarActionLabel,
} from "../AdsTableToolbar"
import { columnsFor, useMetricCatalog } from "../metricCatalog"
import {
  type AdsDimension,
  type ParentLink,
  rowAdvertiserId,
  rowObjectId,
} from "../search"
import { statusLabel } from "../statusLabel"
import { HistoryTab } from "./HistoryTab"
import { MaterialsTab } from "./MaterialsTab"
import { OverviewTab } from "./OverviewTab"
import { SettingsTab } from "./SettingsTab"
import { TrendTab } from "./TrendTab"

const KIND_LABELS: Record<AdsDimension, string> = {
  account: "账户",
  campaign: "系列",
  adgroup: "广告组",
  ad: "广告",
  material: "素材",
  drama: "剧",
}
// 只有系列、广告组、广告有目录配置与管理任务记录，才请求对象详情接口。
type DetailKind = "campaign" | "adgroup" | "ad"
const DETAIL_KINDS = new Set<AdsDimension>(["campaign", "adgroup", "ad"])

export type AdsDetailSheetProps = {
  row: ReportRow | null
  dimension: AdsDimension
  startDate: string
  endDate: string
  readonly: boolean
  snapshotId?: string | null
  onClose: () => void
  onAction: (row: ReportRow, action: ManagementAction) => void
  onOpenParent: (link: ParentLink, advertiserId: string) => void
}

export function AdsDetailSheet({
  row,
  dimension,
  startDate,
  endDate,
  readonly,
  snapshotId,
  onClose,
  onAction,
  onOpenParent,
}: AdsDetailSheetProps) {
  const { tenantId, bc } = useTenantScope()
  const bcId = bc?.bc_id
  const catalog = useMetricCatalog()
  const columns = columnsFor(catalog.data ?? [], dimension)
  const kind = DETAIL_KINDS.has(dimension) ? (dimension as DetailKind) : null
  const remoteId = row ? rowObjectId(row, dimension) : null
  const advertiserId = row ? rowAdvertiserId(row) : ""
  const detailQuery = useQuery({
    // 键包含 BC：切换 BC 后不会显示旧 BC 的对象详情。
    queryKey: [
      "tenant",
      tenantId,
      "ads",
      "detail",
      bcId,
      snapshotId ?? null,
      kind,
      advertiserId,
      remoteId,
    ],
    enabled: Boolean(tenantId && bcId && kind && remoteId && advertiserId),
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.adDetail({
          path: {
            tenant_id: tenantId!,
            kind: kind!,
            remote_id: remoteId!,
          },
          query: {
            advertiser_id: advertiserId,
            bc_id: bcId!,
            snapshot_id: snapshotId ?? undefined,
          },
          signal,
        })
      ).data,
  })
  const detail = detailQuery.data
  const name = row ? (detail?.name ?? rowDisplayName(row, dimension)) : ""
  const status = statusLabel(row?.display ?? {})
  const actions = readonly ? [] : actionsFor(dimension)
  // 素材挂在广告下面：只有广告（及素材自身）显示素材页签，系列、广告组不显示。
  const hasMaterials = dimension === "ad" || dimension === "material"
  return (
    <Sheet
      open={row !== null}
      onOpenChange={(open) => {
        if (!open) onClose()
      }}
    >
      <SheetContent
        side="right"
        className="w-full overflow-y-auto sm:max-w-none md:w-[60vw] md:min-w-[720px]"
      >
        {row && (
          <>
            <SheetHeader
              className="gap-2 pr-10"
              data-testid="ads-detail-header"
            >
              <SheetTitle className="break-words">{name}</SheetTitle>
              <SheetDescription className="flex flex-wrap items-center gap-2">
                <span>{KIND_LABELS[dimension]}</span>
                {remoteId && <span>· {remoteId}</span>}
                <Badge variant={status.tone} title={status.raw}>
                  {status.label}
                </Badge>
              </SheetDescription>
              {actions.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {actions.map((key) => (
                    <Button
                      key={key}
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => onAction(row, toolbarAction(key))}
                    >
                      {toolbarActionLabel(key)}
                    </Button>
                  ))}
                </div>
              )}
            </SheetHeader>
            {/* 以行键为 key：切换到另一个对象时页签与区间回到默认。 */}
            <Tabs
              key={`${dimension}:${row.row_key}`}
              defaultValue="overview"
              className="px-4 pb-6"
            >
              <TabsList>
                <TabsTrigger value="overview">概览</TabsTrigger>
                <TabsTrigger value="trend">趋势</TabsTrigger>
                {kind && <TabsTrigger value="settings">设置</TabsTrigger>}
                {hasMaterials && (
                  <TabsTrigger value="materials">素材</TabsTrigger>
                )}
                {kind && <TabsTrigger value="history">操作记录</TabsTrigger>}
              </TabsList>
              <TabsContent value="overview" className="mt-4">
                <OverviewTab
                  row={row}
                  dimension={dimension}
                  name={name}
                  detail={detail}
                  loading={Boolean(kind) && detailQuery.isLoading}
                  columns={columns}
                  onOpenParent={onOpenParent}
                />
              </TabsContent>
              <TabsContent value="trend" className="mt-4">
                <TrendTab
                  row={row}
                  dimension={dimension}
                  startDate={startDate}
                  endDate={endDate}
                  columns={columns}
                />
              </TabsContent>
              {kind && (
                <TabsContent value="settings" className="mt-4">
                  <DetailState query={detailQuery}>
                    {detail && (
                      <SettingsTab
                        kind={kind}
                        configuration={detail.configuration ?? {}}
                        observedAt={detail.observed_at}
                      />
                    )}
                  </DetailState>
                </TabsContent>
              )}
              {hasMaterials && (
                <TabsContent value="materials" className="mt-4">
                  {dimension === "ad" ? (
                    <DetailState query={detailQuery}>
                      <MaterialsTab materials={detail?.materials ?? []} />
                    </DetailState>
                  ) : (
                    <MaterialsTab
                      materials={(row.material_uses ?? []).map((use) => ({
                        use_ref: use,
                        name: row.display?.material_name ?? "",
                        external: true,
                      }))}
                    />
                  )}
                </TabsContent>
              )}
              {kind && tenantId && bcId && (
                <TabsContent value="history" className="mt-4">
                  <DetailState query={detailQuery}>
                    <HistoryTab
                      tenantId={tenantId}
                      bcId={bcId}
                      items={detail?.operation_history ?? []}
                    />
                  </DetailState>
                </TabsContent>
              )}
            </Tabs>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}

function DetailState({
  query,
  children,
}: {
  query: { isLoading: boolean; isError: boolean; refetch: () => unknown }
  children: ReactNode
}) {
  if (query.isLoading) return <Skeleton className="h-40 w-full" />
  if (query.isError)
    return (
      <div className="flex items-center gap-3 text-sm">
        <p className="text-destructive">对象详情读取失败</p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => void query.refetch()}
        >
          重试
        </Button>
      </div>
    )
  return <>{children}</>
}
