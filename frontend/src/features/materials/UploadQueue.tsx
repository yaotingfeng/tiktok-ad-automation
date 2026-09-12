import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { AxiosError } from "axios"
import { useEffect, useRef, useState } from "react"
import { type IngestFilePublic, MaterialIngestService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Progress } from "@/components/ui/progress"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  isForbidden,
  Pager,
  RequestError,
  ServerTable,
  useRetainedData,
} from "@/features/tenants/shared"
import { canRestartOriginal } from "./ingest-transfer"
import { LegacyUploadQueue } from "./LegacyUploadQueue"
import { bytes, issue, materialKey, Stage } from "./presentation"
import { useIngestPage } from "./useIngestPage"
import { type UploadManager, useFileProgress } from "./useUploadManager"

function TransferCell({
  file,
  manager,
}: {
  file: IngestFilePublic
  manager: UploadManager
}) {
  const progress = useFileProgress(manager, file.client_index)
  const received = Math.max(file.received_bytes, progress?.receivedBytes ?? 0)
  return (
    <div className="flex min-w-40 max-w-64 flex-col gap-1 whitespace-normal">
      <Progress
        value={Math.min(100, (100 * received) / file.size)}
        aria-label={`${file.file_name} 接收进度`}
      />
      <span className="text-xs text-muted-foreground">
        {bytes(received)} / {bytes(file.size)}
      </span>
      {progress?.errorCode && (
        <span className="text-xs text-muted-foreground">
          {issue(progress.errorCode)}
        </span>
      )}
    </div>
  )
}
export function UploadQueue({
  tenantId,
  bcId,
  batchId,
  manager,
  write,
  onDetails,
  onForbidden,
  onHistory,
}: {
  tenantId: string
  bcId: string
  batchId: string
  manager: UploadManager
  write: boolean
  onDetails: (id: string) => void
  onForbidden: () => void
  onHistory: () => void
}) {
  const paging = useIngestPage()
  const [status, setStatus] = useState("all")
  const select = useRef<HTMLInputElement>(null)
  const single = useRef<HTMLInputElement>(null)
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const summaryQuery = useQuery({
    queryKey: [...materialKey(tenantId, bcId), "ingest-summary", batchId],
    queryFn: async ({ signal }) => {
      const summary = (
        await MaterialIngestService.readIngestSummary({
          path: { tenant_id: tenantId, session_id: batchId },
          signal,
        })
      ).data
      if (summary.bc_id !== bcId) throw new Error("scope_mismatch")
      return summary
    },
    refetchInterval: (query) => (query.state.error ? false : 3000),
  })
  const summary = useRetainedData(summaryQuery.data, summaryQuery.error)
  const query = useQuery({
    enabled: !!summaryQuery.data && !summaryQuery.error,
    queryKey: [
      ...materialKey(tenantId, bcId),
      "ingest-files",
      batchId,
      status,
      paging.cursor,
      paging.limit,
    ],
    queryFn: async ({ signal }) =>
      (
        await MaterialIngestService.listIngestFiles({
          path: { tenant_id: tenantId, session_id: batchId },
          query: {
            cursor: paging.cursor,
            limit: paging.limit,
            status: status === "all" ? undefined : status,
          },
          signal,
        })
      ).data,
    refetchInterval: (query) => (query.state.error ? false : 3000),
  })
  const data = useRetainedData(query.data, query.error)
  useEffect(() => {
    if (isForbidden(summaryQuery.error) || isForbidden(query.error))
      onForbidden()
  }, [summaryQuery.error, query.error, onForbidden])
  useEffect(() => {
    if (summaryQuery.data && !summaryQuery.error)
      void manager.control.openSession(summaryQuery.data).catch(() => {})
  }, [summaryQuery.data, summaryQuery.error, manager.control])
  useEffect(() => {
    if (data?.items)
      void manager.control.observePage(batchId, data.items).catch(() => {})
  }, [data, batchId, manager.control])
  const busy = manager.creating || manager.transferring
  const columns: ColumnDef<IngestFilePublic>[] = [
    {
      header: "原文件",
      cell: ({ row }) => (
        <span className="block min-w-52 max-w-80 wrap-anywhere whitespace-normal">
          {row.original.file_name}
        </span>
      ),
    },
    {
      header: "接收进度",
      cell: ({ row }) => <TransferCell file={row.original} manager={manager} />,
    },
    {
      header: "平台入库",
      cell: ({ row }) => <Stage status={row.original.platform_status} />,
    },
    {
      header: "临时原件",
      cell: ({ row }) => (
        <Stage status={row.original.temporary_storage_status} />
      ),
    },
    {
      header: "实际源账户",
      cell: ({ row }) => (
        <span className="font-mono text-xs">
          {row.original.source_advertiser_id || "尚未入库"}
        </span>
      ),
    },
    {
      header: "处理说明",
      cell: ({ row }) =>
        row.original.error_code
          ? issue(row.original.error_code)
          : row.original.operation_status === "result_unknown"
            ? "结果待核实，不会重复上传"
            : "—",
    },
    {
      header: "操作",
      cell: ({ row }) => (
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => onDetails(row.original.material_id)}
          >
            查看详情
          </Button>
          {write && row.original.received_bytes < row.original.size && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => {
                setSelectedIndex(row.original.client_index)
                single.current?.click()
              }}
            >
              重选此文件
            </Button>
          )}
          {write &&
            summary &&
            row.original.received_bytes < row.original.size &&
            row.original.operation_status === "idle" &&
            ["waiting_capacity", "receiving"].includes(
              row.original.temporary_storage_status,
            ) && (
              <Button
                size="sm"
                variant="ghost"
                disabled={manager.creating}
                onClick={() =>
                  void manager.control
                    .cancel(summary, row.original)
                    .then(() => {
                      void query.refetch()
                      void summaryQuery.refetch()
                    })
                }
              >
                放弃此文件
              </Button>
            )}
          {write && canRestartOriginal(row.original) && summary && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() =>
                void manager
                  .retry(summary, row.original)
                  .then(() => query.refetch())
              }
            >
              重试此文件
            </Button>
          )}
        </div>
      ),
    },
  ]
  if (
    summaryQuery.error instanceof AxiosError &&
    summaryQuery.error.response?.status === 404
  )
    return (
      <LegacyUploadQueue
        tenantId={tenantId}
        bcId={bcId}
        batchId={batchId}
        onDetails={onDetails}
        onForbidden={onForbidden}
        onHistory={onHistory}
      />
    )
  if (summaryQuery.error)
    return (
      <RequestError
        error={summaryQuery.error}
        retry={() => void summaryQuery.refetch()}
      />
    )
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Button variant="outline" onClick={onHistory}>
          返回导入历史
        </Button>
        <Button
          variant="outline"
          onClick={() => {
            void summaryQuery.refetch()
            void query.refetch()
          }}
        >
          刷新进度
        </Button>
      </div>
      {manager.pausedAfterCancellation && !manager.creating && (
        <Alert>
          <AlertDescription>
            本次传输已暂停。其余未完成文件保留进度，可点“继续传输 /
            核实接收”恢复。
          </AlertDescription>
        </Alert>
      )}
      {summary && (
        <Alert>
          <AlertDescription>
            <p>
              导入 {summary.expected_count} 个文件 ·{" "}
              {bytes(summary.total_bytes)}
            </p>
            <p>
              已登记 {summary.accepted_count} · 已接收 {summary.uploaded_count}{" "}
              · 平台可用 {summary.ready_count} · 已清理 {summary.cleaned_count}{" "}
              · 失败 {summary.failed_count}
            </p>
            <p>
              当前暂存占用 {bytes(summary.reserved_bytes)}，其中已存储{" "}
              {bytes(summary.stored_bytes)}
              。临时原件清理不影响已经核实的账户素材。
            </p>
          </AlertDescription>
        </Alert>
      )}
      {write && summary && (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            disabled={busy || !!manager.pending}
            onClick={() => void manager.resume(summary)}
          >
            继续传输 / 核实接收
          </Button>
          <Button
            variant="outline"
            disabled={busy || !!manager.pending}
            onClick={() => select.current?.click()}
          >
            重新选择未传完的文件
          </Button>
          <Input
            ref={select}
            className="hidden"
            aria-label="重新选择未传完的文件"
            type="file"
            multiple
            accept="video/*"
            onChange={(event) => {
              const files = Array.from(event.target.files ?? [])
              event.target.value = ""
              if (files.length) void manager.resume(summary, files)
            }}
          />
          {manager.transferring && (
            <Button variant="outline" onClick={manager.control.pause}>
              暂停本地传输
            </Button>
          )}
        </div>
      )}
      {!!manager.error && (
        <Alert variant="destructive">
          <AlertDescription>{issue(manager.error)}</AlertDescription>
        </Alert>
      )}
      <Input
        ref={single}
        className="hidden"
        aria-label="重选单个原文件"
        type="file"
        accept="video/*"
        onChange={(event) => {
          const files = Array.from(event.target.files ?? [])
          event.target.value = ""
          if (summary && selectedIndex !== null && files.length)
            void manager.control.reselectFile(summary, selectedIndex, files)
        }}
      />
      <p className="text-sm text-muted-foreground">
        刷新后浏览器不能继续读取原文件，未传完的文件需重新选择并核验。暂存空间不足时保留排队信息，空间释放后继续。
      </p>
      <Card className="min-w-0">
        <CardHeader>
          <Select
            value={status}
            onValueChange={(value) => {
              setStatus(value)
              paging.reset()
            }}
          >
            <SelectTrigger aria-label="筛选导入状态" className="w-56">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="all">全部文件</SelectItem>
                <SelectItem value="registered">待接收</SelectItem>
                <SelectItem value="waiting_capacity">等待暂存空间</SelectItem>
                <SelectItem value="receiving">正在接收</SelectItem>
                <SelectItem value="failed">失败</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
        </CardHeader>
        <CardContent className="min-w-0">
          <ServerTable
            rows={data?.items ?? []}
            columns={columns}
            loading={query.isPending && !data}
            // 后台轮询保留当前行与布局，不订阅 isFetching 反复重建单元格。
            fetching={query.isPending}
            showRefreshStatus={false}
            error={query.error}
            retry={() => void query.refetch()}
            filtered={status !== "all"}
            emptyTitle="暂无已登记文件"
          />
        </CardContent>
        <CardFooter className="block">
          <Pager
            paging={paging}
            nextCursor={data?.next_cursor}
            busy={query.isPending}
          />
        </CardFooter>
      </Card>
    </div>
  )
}
