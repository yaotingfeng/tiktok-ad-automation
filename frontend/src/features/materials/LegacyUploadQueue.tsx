import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect } from "react"
import { MaterialsService, type UploadFileResult } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import {
  isForbidden,
  ServerTable,
  useRetainedData,
} from "@/features/tenants/shared"
import { bytes, issue, materialKey, Stage } from "./presentation"

/** Historical <=200-file batches are readable, but never resume via old writes. */
export function LegacyUploadQueue({
  tenantId,
  bcId,
  batchId,
  onDetails,
  onForbidden,
  onHistory,
}: {
  tenantId: string
  bcId: string
  batchId: string
  onDetails: (id: string) => void
  onForbidden: () => void
  onHistory: () => void
}) {
  const query = useQuery({
    queryKey: [...materialKey(tenantId, bcId), "legacy-batch", batchId],
    queryFn: async ({ signal }) => {
      const result = (
        await MaterialsService.readUploadBatch({
          path: { tenant_id: tenantId, batch_id: batchId },
          signal,
        })
      ).data
      if (result.bc_id !== bcId) throw new Error("scope_mismatch")
      return result
    },
  })
  const data = useRetainedData(query.data, query.error)
  useEffect(() => {
    if (isForbidden(query.error)) onForbidden()
  }, [query.error, onForbidden])
  const columns: ColumnDef<UploadFileResult>[] = [
    {
      header: "原文件",
      cell: ({ row }) => (
        <span className="block min-w-52 max-w-80 wrap-anywhere whitespace-normal">
          {row.original.file_name}
        </span>
      ),
    },
    { header: "文件大小", cell: ({ row }) => bytes(row.original.byte_size) },
    {
      header: "素材状态",
      cell: ({ row }) => <Stage status={row.original.status} />,
    },
    {
      header: "实际源账户",
      cell: ({ row }) => (
        <span className="font-mono text-xs">
          {row.original.latest_advertiser_id || "尚未入库"}
        </span>
      ),
    },
    {
      header: "处理说明",
      cell: ({ row }) =>
        row.original.error_code ? issue(row.original.error_code) : "—",
    },
    {
      header: "操作",
      cell: ({ row }) => (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => onDetails(row.original.material_id)}
        >
          查看详情
        </Button>
      ),
    },
  ]
  return (
    <Card className="min-w-0">
      <CardHeader className="gap-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          {data && <Stage status={data.status} />}
          <Button variant="outline" onClick={onHistory}>
            返回导入历史
          </Button>
        </div>
        <Alert>
          <AlertTitle>历史批次仅供查看</AlertTitle>
          <AlertDescription>
            保留原批次状态和实际来源。新文件请使用批量上传建立导入会话。
          </AlertDescription>
        </Alert>
      </CardHeader>
      <CardContent className="min-w-0">
        <ServerTable
          rows={data?.files || []}
          columns={columns}
          loading={query.isPending && !data}
          fetching={query.isFetching}
          error={query.error}
          retry={() => void query.refetch()}
          filtered={false}
          emptyTitle="此历史批次没有文件"
        />
      </CardContent>
    </Card>
  )
}
