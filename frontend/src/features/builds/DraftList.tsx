import { useQuery } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { BuildsService, type DraftListItem } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { displayTime } from "@/features/accounts/presentation"
import {
  isForbidden,
  Pager,
  ServerTable,
  useCursorPage,
} from "@/features/tenants/shared"

function draftStatus(row: DraftListItem) {
  if (row.preview_status === "FROZEN") return "待提交"
  if (row.preview_status === "BUILDING") return "预览生成中"
  if (row.preview_status === "FAILED" || row.preview_status === "OBSOLETE")
    return "待处理"
  return {
    DRAFT: "已保存",
    PREPARING: "准备中",
    BLOCKED: "待处理",
    READY: "可预览",
  }[row.status]
}

export function DraftList({
  tenantId,
  bcId,
  write,
  onResume,
}: {
  tenantId: string
  bcId: string
  write: boolean
  onResume: () => void
}) {
  const paging = useCursorPage()
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "build-drafts",
      bcId,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await BuildsService.listDrafts({
          path: { tenant_id: tenantId },
          query: { bc_id: bcId, cursor: paging.cursor, limit: paging.limit },
          signal,
        })
      ).data,
    // 更新排序由用户刷新，避免后台准备不断把正在操作的行移走。
    refetchOnWindowFocus: false,
  })
  const columns: ColumnDef<DraftListItem>[] = [
    {
      header: "剧目摘要",
      cell: ({ row: { original: row } }) => (
        <div className="flex min-w-48 max-w-80 flex-col gap-1 whitespace-normal">
          <span className="break-words">
            {row.drama_titles.join("、") || "暂无剧名"}
          </span>
          <span className="text-xs text-muted-foreground">
            已输入 {row.drama_input_count} 行剧名 · v{row.revision}
          </span>
        </div>
      ),
    },
    {
      header: "账户",
      cell: ({ row: { original: row } }) => (
        <div className="flex flex-col gap-1">
          <span>已输入 {row.account_input_count} 行</span>
          <span className="text-xs text-muted-foreground">
            已解析 {row.resolved_account_count} 个账户
          </span>
        </div>
      ),
    },
    {
      header: "策略",
      cell: ({ row: { original: row } }) => (
        <span className="block max-w-56 whitespace-normal break-words">
          {row.strategy_label}
        </span>
      ),
    },
    {
      header: "状态",
      cell: ({ row: { original: row } }) => (
        <Badge variant="secondary">{draftStatus(row)}</Badge>
      ),
    },
    {
      header: "最近修改",
      cell: ({ row: { original: row } }) => displayTime(row.updated_at),
    },
    {
      header: "操作",
      cell: ({ row: { original: row } }) => {
        const preview =
          row.preview_id &&
          ["BUILDING", "FROZEN"].includes(row.preview_status || "")
        // 恢复只导航到原记录，不携带 prepare 参数，不发起任何搭建写入。
        return (
          <Button variant="outline" size="sm" asChild>
            {preview ? (
              <Link
                onClick={onResume}
                to="/tenants/$tenantId/build-previews/$previewId"
                params={{ tenantId, previewId: row.preview_id! }}
                search={{ bc_id: bcId }}
              >
                {write ? "继续搭建" : "查看详情"}
              </Link>
            ) : (
              <Link
                onClick={onResume}
                to="/tenants/$tenantId/build-drafts/$draftId"
                params={{ tenantId, draftId: row.draft_id }}
                search={{
                  bc_id: bcId,
                  edit: row.status === "DRAFT" ? true : undefined,
                }}
              >
                {write ? "继续搭建" : "查看详情"}
              </Link>
            )}
          </Button>
        )
      },
    },
  ]
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex justify-end">
        <Button
          variant="outline"
          disabled={query.isFetching}
          onClick={() => {
            paging.reset()
            if (paging.page === 1) void query.refetch()
          }}
        >
          刷新列表
        </Button>
      </div>
      <ServerTable
        rows={isForbidden(query.error) ? [] : query.data?.items || []}
        columns={columns}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={false}
        emptyTitle="草稿箱为空"
      />
      <div className="w-full">
        <Pager
          paging={paging}
          nextCursor={query.data?.next_cursor}
          total={query.data?.total}
          busy={query.isFetching}
        />
      </div>
    </div>
  )
}
