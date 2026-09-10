import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect } from "react"
import { type IngestSummary, MaterialIngestService } from "@/client"
import { Button } from "@/components/ui/button"
import { displayTime } from "@/features/accounts/presentation"
import {
  isForbidden,
  Pager,
  ServerTable,
  useRetainedData,
} from "@/features/tenants/shared"
import { materialKey, Stage } from "./presentation"
import { useIngestPage } from "./useIngestPage"
export function BatchHistory({
  tenantId,
  bcId,
  onOpen,
  enabled,
  onForbidden,
}: {
  tenantId: string
  bcId: string
  onOpen: (id: string) => void
  enabled: boolean
  onForbidden: () => void
}) {
  const paging = useIngestPage(),
    query = useQuery({
      enabled,
      queryKey: [
        ...materialKey(tenantId, bcId),
        "ingest-sessions",
        paging.cursor,
        paging.limit,
      ],
      queryFn: async ({ signal }) =>
        (
          await MaterialIngestService.listIngestSessions({
            path: { tenant_id: tenantId },
            query: { bc_id: bcId, cursor: paging.cursor, limit: paging.limit },
            signal,
          })
        ).data,
    }),
    data = useRetainedData(query.data, query.error)
  useEffect(() => {
    if (isForbidden(query.error)) onForbidden()
  }, [query.error, onForbidden])
  const columns: ColumnDef<IngestSummary>[] = [
    {
      header: "导入会话",
      cell: ({ row }) => (
        <Button variant="link" onClick={() => onOpen(row.original.session_id)}>
          {row.original.session_id}
        </Button>
      ),
    },
    {
      header: "文件数量",
      cell: ({ row }) => `${row.original.expected_count} 个文件`,
    },
    {
      header: "独立进度",
      cell: ({ row }) =>
        `已接收 ${row.original.uploaded_count} · 可用 ${row.original.ready_count} · 已清理 ${row.original.cleaned_count}`,
    },
    {
      header: "当前阶段",
      cell: ({ row }) => <Stage status={row.original.status} />,
    },
    {
      header: "建立时间",
      cell: ({ row }) => displayTime(row.original.created_at),
    },
    {
      header: "操作",
      cell: ({ row }) => (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onOpen(row.original.session_id)}
        >
          查看上传队列
        </Button>
      ),
    },
  ]
  return (
    <>
      <ServerTable
        rows={data?.items || []}
        columns={columns}
        loading={query.isPending && !data}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={false}
        emptyTitle="暂无导入会话"
      />
      <Pager
        paging={paging}
        nextCursor={data?.next_cursor}
        busy={query.isFetching}
      />
    </>
  )
}
