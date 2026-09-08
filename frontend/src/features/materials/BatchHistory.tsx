import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect } from "react"
import { MaterialsService, type UploadBatchSummary } from "@/client"
import { Button } from "@/components/ui/button"
import { displayTime } from "@/features/accounts/presentation"
import {
  isForbidden,
  Pager,
  ServerTable,
  useCursorPage,
  useRetainedData,
} from "@/features/tenants/shared"
import { materialKey, Stage } from "./presentation"
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
  const paging = useCursorPage(),
    query = useQuery({
      enabled,
      queryKey: [
        ...materialKey(tenantId, bcId),
        "batches",
        paging.cursor,
        paging.limit,
      ],
      queryFn: async ({ signal }) =>
        (
          await MaterialsService.readUploadBatches({
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
  const columns: ColumnDef<UploadBatchSummary>[] = [
    {
      header: "上传批次",
      cell: ({ row }) => (
        <Button variant="link" onClick={() => onOpen(row.original.batch_id)}>
          {row.original.batch_id}
        </Button>
      ),
    },
    {
      header: "文件数量",
      cell: ({ row }) => `${row.original.file_count} 个文件`,
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
          onClick={() => onOpen(row.original.batch_id)}
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
        emptyTitle="暂无上传批次"
      />
      <Pager
        paging={paging}
        nextCursor={data?.next_cursor}
        busy={query.isFetching}
      />
    </>
  )
}
