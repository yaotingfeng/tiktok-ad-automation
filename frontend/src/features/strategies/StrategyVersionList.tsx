import { useQuery } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { useMemo } from "react"
import { StrategiesService, type VersionPublic } from "@/client"
import { Button } from "@/components/ui/button"
import { displayTime } from "@/features/accounts/presentation"
import { normalizeDecimal } from "@/features/strategies/validation"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  Pager,
  ServerTable,
  useCursorPage,
  useRetainedData,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { strategyKey } from "./queries"
export function StrategyVersionList({
  strategyId,
  name,
  onClose,
}: {
  strategyId: string
  name: string
  onClose: () => void
}) {
  const { tenantId, scope } = useTenantScope(),
    paging = useCursorPage(),
    navigate = useNavigate()
  const write = !!scope && scope.role !== "viewer"
  const query = useQuery({
    queryKey: [
      ...strategyKey(tenantId!),
      "history",
      strategyId,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await StrategiesService.versions({
          path: { tenant_id: tenantId!, strategy_id: strategyId },
          query: { cursor: paging.cursor, limit: paging.limit },
          signal,
        })
      ).data,
  })
  const data = useRetainedData(query.data, query.error)
  const columns = useMemo<ColumnDef<VersionPublic>[]>(
    () => [
      { header: "版本", cell: ({ row }) => `v${row.original.number}` },
      {
        header: "预算 / ROAS",
        cell: ({ row: { original: r } }) => (
          <div>
            {r.config.currency} {normalizeDecimal(r.config.budget)}
            <p>ROAS {r.config.target_roas} 倍</p>
          </div>
        ),
      },
      {
        header: "素材 / 创意",
        cell: ({ row }) =>
          `${row.original.config.group_size} 条素材/组 · ${row.original.config.creative_count} 条创意/组`,
      },
      {
        header: "保存信息",
        cell: ({ row }) => (
          <div>
            {displayTime(row.original.created_at)}
            <p className="text-xs">{row.original.created_by}</p>
          </div>
        ),
      },
      {
        header: "操作",
        cell: ({ row }) => (
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                onClose()
                void navigate({
                  to: "/tenants/$tenantId/strategies/$strategyId",
                  params: { tenantId: tenantId!, strategyId },
                  search: {
                    bc_id: scope?.bcId || undefined,
                    version_id: row.original.id,
                    readonly: true,
                  },
                })
              }}
            >
              查看版本
            </Button>
            {write && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  onClose()
                  void navigate({
                    to: "/tenants/$tenantId/strategies/new",
                    params: { tenantId: tenantId! },
                    search: {
                      bc_id: scope?.bcId || undefined,
                      copy_version_id: row.original.id,
                    },
                  })
                }}
              >
                复制为新策略
              </Button>
            )}
          </div>
        ),
      },
    ],
    [navigate, onClose, scope?.bcId, tenantId, strategyId, write],
  )
  return (
    <ManagementSheet
      title="策略版本历史"
      description={`${name} · 每个版本不可变，旧任务继续使用原版本。`}
      dirty={false}
      onClose={onClose}
    >
      <div className="flex min-w-0 flex-col gap-4">
        <ServerTable
          rows={data?.items || []}
          columns={columns}
          loading={query.isPending && !data}
          fetching={query.isFetching}
          error={query.error}
          retry={() => void query.refetch()}
          filtered={false}
          emptyTitle="暂无策略版本"
        />
        <Pager
          paging={paging}
          nextCursor={data?.next_cursor}
          busy={query.isFetching}
        />
      </div>
    </ManagementSheet>
  )
}
