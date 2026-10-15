import { useQuery } from "@tanstack/react-query"
import { useState } from "react"
import {
  type AdMaterialPublic,
  AdsReportingService,
  type MaterialUsePublic,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { type ColumnDef, formatMetric } from "../metricCatalog"
import { SortableHead } from "../SortableHead"

const STATUS: Record<string, string> = {
  ENABLE: "已开启",
  DISABLE: "已暂停",
  DELETE: "已删除",
}

function statusText(status: string | null | undefined) {
  return status ? (STATUS[status] ?? status) : "—"
}

/** 素材维度详情：只列出该素材的使用位置（数据在素材行本身）。 */
export function MaterialsTab({
  materials,
}: {
  materials: MaterialUsePublic[]
}) {
  if (!materials.length)
    return (
      <p className="text-sm text-muted-foreground">
        暂无素材使用记录；外部广告仍保留。
      </p>
    )
  return (
    <div className="overflow-x-auto rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>账户</TableHead>
            <TableHead>广告</TableHead>
            <TableHead>名称</TableHead>
            <TableHead>状态</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {materials.map((material) => (
            <TableRow
              key={`${material.use_ref.ad_ref.advertiser_id}:${material.use_ref.ad_ref.remote_id}:${material.use_ref.ad_material_id ?? ""}`}
            >
              <TableCell>{material.use_ref.ad_ref.advertiser_id}</TableCell>
              <TableCell>{material.use_ref.ad_ref.remote_id}</TableCell>
              <TableCell className="max-w-64 truncate" title={material.name}>
                {material.name || material.use_ref.platform_material_id}
              </TableCell>
              <TableCell>{statusText(material.operation_status)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

const AD_MATERIAL_COLUMNS = new Set([
  "spend",
  "impressions",
  "clicks",
  "revenue_d0",
  "roas_d0",
  "revenue_total",
  "roas_total",
])

/**
 * 广告详情的素材：每个素材在这条广告里的数据（来自素材日报，按帖子归到本广告），
 * 勾选后只启停本广告里的这些素材，不影响同一视频在其他广告的投放。
 */
export function AdMaterialsTab({
  advertiserId,
  adId,
  startDate,
  endDate,
  columns,
  readonly,
  onToggle,
}: {
  advertiserId: string
  adId: string
  startDate: string
  endDate: string
  columns: ColumnDef[]
  readonly: boolean
  onToggle: (adMaterialIds: string[], value: "ENABLE" | "DISABLE") => void
}) {
  const { tenantId, bc } = useTenantScope()
  const bcId = bc?.bc_id
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [sort, setSort] = useState<{ by: string; direction: "asc" | "desc" }>({
    by: "spend",
    direction: "desc",
  })
  const onSort = (by: string) =>
    setSort((current) => ({
      by,
      direction:
        current.by === by && current.direction === "desc" ? "asc" : "desc",
    }))
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "ads",
      "ad-materials",
      bcId,
      advertiserId,
      adId,
      startDate,
      endDate,
    ],
    enabled: Boolean(tenantId && bcId && advertiserId && adId),
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.adMaterials({
          path: { tenant_id: tenantId!, remote_id: adId },
          query: {
            advertiser_id: advertiserId,
            bc_id: bcId!,
            start_date: startDate,
            end_date: endDate,
          },
          signal,
        })
      ).data,
  })
  if (query.isLoading) return <Skeleton className="h-40 w-full" />
  if (query.isError)
    return (
      <div className="flex items-center gap-3 text-sm">
        <p className="text-destructive">素材数据读取失败</p>
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
  const items = query.data?.items ?? []
  if (!items.length)
    return (
      <p className="text-sm text-muted-foreground">
        暂无素材使用记录；外部广告仍保留。
      </p>
    )
  const shown = columns.filter((column) => AD_MATERIAL_COLUMNS.has(column.key))
  // 接口一次返回本广告全部素材；按展示所用的原始数值排序，避免字符串/格式化值误排。
  // 缺失或非有限数值在两个方向都置底；相同值保持接口顺序，且不修改查询缓存。
  const metricValue = (item: AdMaterialPublic) => {
    const raw = item.metric_buckets?.[0]?.values?.[sort.by]
    if (raw == null || raw.trim() === "") return null
    const value = Number(raw)
    return Number.isFinite(value) ? value : null
  }
  const sortedItems = [...items].sort((left, right) => {
    const a = metricValue(left)
    const b = metricValue(right)
    if (a === null) return b === null ? 0 : 1
    if (b === null) return -1
    return sort.direction === "asc" ? a - b : b - a
  })
  const key = (item: AdMaterialPublic) => item.use_ref.ad_material_id ?? ""
  const selectable = items.filter((item) => key(item))
  const allChecked =
    selectable.length > 0 && selectable.every((item) => selected.has(key(item)))
  const toggle = (id: string, checked: boolean) =>
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  return (
    <div className="space-y-3">
      {!readonly && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted-foreground">
            已选 {selected.size} 个素材（只作用于本广告）
          </span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={selected.size === 0}
            onClick={() => onToggle([...selected], "ENABLE")}
          >
            启用
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={selected.size === 0}
            onClick={() => onToggle([...selected], "DISABLE")}
          >
            暂停
          </Button>
        </div>
      )}
      <div className="overflow-x-auto rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              {!readonly && (
                <TableHead className="w-10">
                  <Checkbox
                    aria-label="全选素材"
                    checked={allChecked}
                    onCheckedChange={(checked) =>
                      setSelected(
                        checked ? new Set(selectable.map(key)) : new Set(),
                      )
                    }
                  />
                </TableHead>
              )}
              <TableHead>名称</TableHead>
              <TableHead>状态</TableHead>
              {shown.map((column) => (
                <TableHead
                  key={column.key}
                  title={column.fullName}
                  aria-sort={
                    sort.by === column.key
                      ? sort.direction === "asc"
                        ? "ascending"
                        : "descending"
                      : "none"
                  }
                >
                  <SortableHead
                    label={column.label}
                    sortBy={column.key}
                    activeSortBy={sort.by}
                    direction={sort.direction}
                    onSort={onSort}
                  />
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {sortedItems.map((item) => {
              const id = key(item)
              const vector = item.metric_buckets?.[0]
              return (
                <TableRow key={`${item.use_ref.platform_material_id}:${id}`}>
                  {!readonly && (
                    <TableCell>
                      <Checkbox
                        aria-label={`选择 ${item.name}`}
                        disabled={!id}
                        checked={selected.has(id)}
                        onCheckedChange={(checked) =>
                          toggle(id, checked === true)
                        }
                      />
                    </TableCell>
                  )}
                  <TableCell className="max-w-64" title={item.name}>
                    <div className="truncate">
                      {item.name || item.use_ref.platform_material_id}
                    </div>
                    {item.shared && (
                      <Badge variant="outline" className="mt-1">
                        多广告共用，数据为合计
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell>{statusText(item.operation_status)}</TableCell>
                  {shown.map((column) => (
                    <TableCell key={column.key} className="tabular-nums">
                      {formatMetric(
                        vector?.values?.[column.key] ?? null,
                        column.unit,
                      )}
                    </TableCell>
                  ))}
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
