import { useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useState } from "react"
import { MaterialsService, type UploadFileResult } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Progress } from "@/components/ui/progress"
import { FilterSelect } from "@/features/accounts/presentation"
import {
  isForbidden,
  Pager,
  ServerTable,
  useCursorPage,
} from "@/features/tenants/shared"
import { bytes, CopyValue, issue, materialKey, Stage } from "./presentation"
import type { UploadManager } from "./useUploadManager"
export function UploadQueue({
  tenantId,
  bcId,
  batchId,
  manager,
  write,
  onDetails,
  onHistory,
  onForbidden,
}: {
  tenantId: string
  bcId: string
  batchId: string
  manager: UploadManager
  write: boolean
  onDetails: (id: string) => void
  onHistory: () => void
  onForbidden: () => void
}) {
  const [filter, setFilter] = useState("all"),
    [retrying, setRetrying] = useState(false),
    cache = useQueryClient(),
    paging = useCursorPage()
  const query = useQuery({
    queryKey: [...materialKey(tenantId, bcId), "batch", batchId],
    queryFn: async ({ signal }) => {
      const { data } = await MaterialsService.readUploadBatch({
        path: { tenant_id: tenantId, batch_id: batchId },
        signal,
      })
      if (data.bc_id !== bcId) throw new Error("batch_scope_mismatch")
      return data
    },
    refetchInterval: (q) =>
      q.state.data?.files.some((f) =>
        ["stored", "uploading", "verifying", "result_unknown"].includes(
          f.status,
        ),
      )
        ? 3000
        : false,
  })
  useEffect(() => {
    if (isForbidden(query.error)) onForbidden()
  }, [query.error, onForbidden])
  const refresh = async () => {
    const result = await query.refetch()
    if (write && result.data && !result.error)
      await manager.confirmCompletion(result.data)
  }
  const data = query.data?.bc_id === bcId ? query.data : undefined
  useEffect(() => {
    if (data) manager.observe(data)
  }, [data, manager.observe])
  useEffect(() => {
    if (data)
      void cache.invalidateQueries({
        queryKey: [...materialKey(tenantId, bcId), "library"],
      })
  }, [data, cache, tenantId, bcId])
  const retryable =
    data?.files.filter((f) => f.can_retry && f.status === "blocked") || []
  const rows =
    data?.files.filter(
      (f) =>
        filter === "all" ||
        (filter === "complete" && f.status === "available") ||
        (filter === "attention" &&
          ["blocked", "result_unknown"].includes(f.status)) ||
        (filter === "pending" &&
          !["available", "blocked", "result_unknown"].includes(f.status)),
    ) || []
  const columns: ColumnDef<UploadFileResult>[] = [
    {
      header: "文件",
      cell: ({ row: { original: r } }) => (
        <div className="min-w-48 max-w-64">
          <p className="truncate" title={r.file_name}>
            {r.file_name}
          </p>
          <p className="text-xs text-muted-foreground">{bytes(r.byte_size)}</p>
        </div>
      ),
    },
    {
      header: "本地传输",
      cell: ({ row: { original: r } }) => {
        const p = manager.progress[r.material_id],
          received =
            r.status === "receiving"
              ? (p?.bytes ?? r.received_bytes)
              : (r.received_bytes ?? p?.bytes)
        return (
          <div className="min-w-40 max-w-64">
            {received != null ? (
              <>
                <p className="text-xs">
                  {bytes(received)} / {bytes(r.byte_size)}
                </p>
                <Progress
                  aria-label={`${r.file_name} 原文件传输`}
                  value={Math.min(100, (received / r.byte_size) * 100)}
                />
              </>
            ) : (
              <p className="text-xs text-muted-foreground">
                尚无已确认传输进度
              </p>
            )}
            {r.status === "receiving" && !p?.busy && (
              <p className="text-xs text-muted-foreground">
                需要继续上传原文件
              </p>
            )}
          </div>
        )
      },
    },
    {
      header: "当前阶段",
      cell: ({ row }) => <Stage status={row.original.status} />,
    },
    {
      header: "实际上传账户",
      cell: ({ row: { original: r } }) =>
        r.latest_advertiser_id ? (
          <CopyValue value={r.latest_advertiser_id} />
        ) : (
          "系统尚未分配"
        ),
    },
    {
      header: "处理信息",
      cell: ({ row: { original: r } }) => (
        <p className="min-w-40 max-w-72 text-xs">
          {r.error_code
            ? issue(r.error_code)
            : manager.progress[r.material_id]?.message ||
              (r.status === "stored"
                ? "原文件已保留，后台继续；不等于账户素材已可用。"
                : r.status === "result_unknown"
                  ? "正在核实外部结果，不能重复上传。"
                  : "查看实际账户记录了解处理结果。")}
        </p>
      ),
    },
    {
      header: "操作",
      cell: ({ row: { original: r } }) => {
        const p = manager.progress[r.material_id],
          unknown = manager.records[r.material_id]?.completionUnknown
        return (
          <div className="flex min-w-40 flex-col items-start gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onDetails(r.material_id)}
            >
              查看记录
            </Button>
            {r.status === "result_unknown" || unknown ? (
              <Button
                variant="outline"
                size="sm"
                onClick={() => void refresh()}
              >
                查看核实进度
              </Button>
            ) : write && r.can_retry && r.status === "blocked" ? (
              <Button
                variant="outline"
                size="sm"
                disabled={retrying || p?.busy}
                onClick={() => void manager.retry(batchId, r)}
              >
                重试
              </Button>
            ) : write && r.status === "receiving" && !p?.busy ? (
              <Field>
                <FieldLabel
                  htmlFor={`resume-${r.material_id}`}
                  className="text-xs"
                >
                  重新选择原文件
                </FieldLabel>
                <Input
                  className="max-w-52 text-xs"
                  type="file"
                  accept="video/*"
                  id={`resume-${r.material_id}`}
                  disabled={
                    Object.values(manager.progress).filter((p) => p.busy)
                      .length >= 2
                  }
                  onChange={(e) => {
                    const file = e.target.files?.[0]
                    if (file) void manager.run(batchId, r, file)
                    e.target.value = ""
                  }}
                />
              </Field>
            ) : null}
          </div>
        )
      },
    },
  ]
  return (
    <>
      <div className="flex flex-wrap items-center gap-3 p-4">
        <Button variant="ghost" onClick={onHistory}>
          全部上传批次
        </Button>
        <p className="min-w-0 break-all text-xs text-muted-foreground">
          本次批次 {batchId}
        </p>
        <FilterSelect
          label="队列状态"
          choices={{
            pending: "处理中",
            attention: "需处理",
            complete: "已完成",
          }}
          value={filter}
          onChange={(value) => {
            setFilter(value)
            paging.reset()
          }}
        />
        <Button
          variant="outline"
          onClick={() => void refresh()}
          disabled={query.isFetching}
        >
          刷新状态
        </Button>
        {write && retryable.length > 0 && (
          <Button
            variant="outline"
            disabled={retrying}
            onClick={async () => {
              setRetrying(true)
              try {
                for (const row of retryable) await manager.retry(batchId, row)
              } finally {
                setRetrying(false)
              }
            }}
          >
            重试明确失败项
          </Button>
        )}
      </div>
      {!!manager.error && (
        <Alert variant="destructive">
          <AlertDescription>{issue(manager.error)}</AlertDescription>
        </Alert>
      )}
      <ServerTable
        rows={rows.slice(
          (paging.page - 1) * paging.limit,
          paging.page * paging.limit,
        )}
        columns={columns}
        loading={query.isPending}
        fetching={query.isFetching}
        error={query.error}
        retry={() => void query.refetch()}
        filtered={filter !== "all"}
        emptyTitle="本批次没有文件"
      />
      <Pager
        paging={paging}
        nextCursor={
          paging.page * paging.limit < rows.length
            ? String(paging.page + 1)
            : null
        }
        busy={query.isFetching}
      />
    </>
  )
}
