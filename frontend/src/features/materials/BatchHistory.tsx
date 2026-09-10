import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect } from "react"
import { type IngestSummary, MaterialIngestService } from "@/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter } from "@/components/ui/card"
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
      id: "session",
      minSize: 360,
      cell: ({ row }) => (
        <Button
          className="h-auto max-w-full justify-start p-0 text-left"
          variant="link"
          onClick={() => onOpen(row.original.session_id)}
        >
          <span className="wrap-anywhere whitespace-normal">
            {row.original.session_id}
          </span>
        </Button>
      ),
    },
    {
      header: "文件数量",
      size: 144,
      cell: ({ row }) => `${row.original.expected_count} 个文件`,
    },
    {
      header: "独立进度",
      size: 360,
      cell: ({ row }) => (
        <span className="whitespace-normal">
          {`已接收 ${row.original.uploaded_count} · 可用 ${row.original.ready_count} · 已清理 ${row.original.cleaned_count}`}
        </span>
      ),
    },
    {
      header: "当前阶段",
      size: 200,
      cell: ({ row }) => <Stage status={row.original.status} />,
    },
    {
      header: "建立时间",
      size: 208,
      cell: ({ row }) => displayTime(row.original.created_at),
    },
    {
      header: "操作",
      size: 160,
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
    <Card className="min-w-0">
      <CardContent className="min-w-0">
        <ServerTable
          fixedLayout={{ fillColumn: "session" }}
          rows={data?.items || []}
          columns={columns}
          loading={query.isPending && !data}
          fetching={query.isFetching}
          error={query.error}
          retry={() => void query.refetch()}
          filtered={false}
          emptyTitle="暂无导入会话"
        />
      </CardContent>
      <CardFooter className="block">
        <Pager
          paging={paging}
          nextCursor={data?.next_cursor}
          busy={query.isFetching}
        />
      </CardFooter>
    </Card>
  )
}
