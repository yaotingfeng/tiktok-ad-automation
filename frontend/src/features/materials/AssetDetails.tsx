import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useRef, useState } from "react"
import {
  type AccountAsset,
  MaterialsService,
  type UploadAttemptPublic,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { displayTime } from "@/features/accounts/presentation"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  isForbidden,
  Pager,
  RequestError,
  ServerTable,
  useCursorPage,
  useRetainedData,
} from "@/features/tenants/shared"
import { handleApiError } from "@/lib/api-feedback"
import { bytes, CopyValue, issue, materialKey, Stage } from "./presentation"
export function AssetDetails({
  tenantId,
  bcId,
  materialId,
  onClose,
  onForbidden,
}: {
  tenantId: string
  bcId: string
  materialId: string
  onClose: () => void
  onForbidden: () => void
}) {
  const [preview, setPreview] = useState<string | null>(null),
    [previewError, setPreviewError] = useState(false),
    [previewLoading, setPreviewLoading] = useState(false),
    previewController = useRef<AbortController | null>(null)
  useEffect(
    () => () => {
      previewController.current?.abort()
    },
    [],
  )
  const path = { tenant_id: tenantId, material_id: materialId },
    key = [...materialKey(tenantId, bcId), "details", materialId]
  const detail = useQuery({
      queryKey: key,
      queryFn: async ({ signal }) =>
        (
          await MaterialsService.getMaterial({
            path,
            query: { bc_id: bcId },
            signal,
          })
        ).data,
    }),
    assetPage = useCursorPage(),
    attemptPage = useCursorPage()
  const assets = useQuery({
      queryKey: [...key, "assets", assetPage.cursor, assetPage.limit],
      queryFn: async ({ signal }) =>
        (
          await MaterialsService.getAssets({
            path,
            query: {
              bc_id: bcId,
              cursor: assetPage.cursor,
              limit: assetPage.limit,
            },
            signal,
          })
        ).data,
    }),
    attempts = useQuery({
      queryKey: [...key, "attempts", attemptPage.cursor, attemptPage.limit],
      queryFn: async ({ signal }) =>
        (
          await MaterialsService.getAttempts({
            path,
            query: {
              bc_id: bcId,
              cursor: attemptPage.cursor,
              limit: attemptPage.limit,
            },
            signal,
          })
        ).data,
    })
  const a = useRetainedData(assets.data, assets.error),
    h = useRetainedData(attempts.data, attempts.error)
  useEffect(() => {
    if ([detail.error, assets.error, attempts.error].some(isForbidden))
      onForbidden()
  }, [detail.error, assets.error, attempts.error, onForbidden])
  const ac: ColumnDef<AccountAsset>[] = [
    {
      header: "实际账户",
      cell: ({ row }) => <CopyValue value={row.original.advertiser_id} />,
    },
    {
      header: "VID / MID",
      cell: ({ row: { original: r } }) => (
        <div>
          <p className="text-xs text-muted-foreground">VID</p>
          <CopyValue value={r.video_id} />
          <p className="text-xs text-muted-foreground">MID</p>
          {r.mid ? <CopyValue value={r.mid} /> : "尚无 MID"}
        </div>
      ),
    },
    {
      header: "状态与回查",
      cell: ({ row }) => (
        <div>
          <Stage status={row.original.status} />
          <p className="text-xs text-muted-foreground">
            {displayTime(row.original.verified_at)}
          </p>
        </div>
      ),
    },
  ]
  const hc: ColumnDef<UploadAttemptPublic>[] = [
    {
      header: "实际上传账户",
      cell: ({ row }) => <CopyValue value={row.original.advertiser_id} />,
    },
    {
      header: "尝试时间",
      cell: ({ row }) => displayTime(row.original.created_at),
    },
    {
      header: "阶段",
      cell: ({ row }) => (
        <div>
          <Stage status={row.original.status} />
          {row.original.error_code && (
            <p className="text-xs">{issue(row.original.error_code)}</p>
          )}
        </div>
      ),
    },
  ]
  return (
    <ManagementSheet
      title="文件与账户记录"
      description={`当前租户 · BC ${bcId}。每次实际来源账户均保留记录，账户资产不代表全部目标账户可用。`}
      dirty={false}
      onClose={onClose}
    >
      <div className="flex flex-col gap-5">
        {detail.isPending ? (
          <Skeleton className="h-24" />
        ) : detail.error ? (
          <RequestError
            error={detail.error}
            retry={() => void detail.refetch()}
          />
        ) : (
          detail.data && (
            <section aria-label="文件信息" className="flex flex-col gap-2">
              <h2 className="font-semibold">原始文件</h2>
              <CopyValue label="文件名" value={detail.data.file_name} />
              <p className="text-sm">
                {bytes(detail.data.byte_size)} · {detail.data.mime_type}
              </p>
              <p className="text-sm text-muted-foreground">
                {detail.data.original_available
                  ? "原文件已完整接收并保留"
                  : "原文件尚未完整接收"}
              </p>
              <Stage status={detail.data.status} />
              {detail.data.original_available && (
                <Button
                  variant="outline"
                  disabled={previewLoading}
                  onClick={async () => {
                    previewController.current?.abort()
                    const c = new AbortController()
                    previewController.current = c
                    setPreviewLoading(true)
                    setPreviewError(false)
                    setPreview(null)
                    try {
                      const { data } =
                        await MaterialsService.readOriginalPreview({
                          path,
                          query: { bc_id: bcId },
                          signal: c.signal,
                        })
                      if (!c.signal.aborted) setPreview(data.url)
                    } catch (e) {
                      if (e instanceof Error) handleApiError(e)
                      if (isForbidden(e)) onForbidden()
                      if (!c.signal.aborted) setPreviewError(true)
                    } finally {
                      if (!c.signal.aborted) setPreviewLoading(false)
                    }
                  }}
                >
                  {previewLoading
                    ? "正在读取原文件…"
                    : preview
                      ? "重新获取预览"
                      : "预览原文件"}
                </Button>
              )}
              {previewError && (
                <Alert variant="destructive">
                  <AlertDescription>
                    原文件预览暂不可用，请重新获取预览。
                  </AlertDescription>
                </Alert>
              )}
              {preview && (
                <video
                  aria-label="原文件视频预览"
                  src={preview}
                  controls
                  preload="metadata"
                  className="max-h-80 w-full"
                  onError={() => {
                    setPreviewError(true)
                    setPreview(null)
                  }}
                >
                  <track kind="captions" />
                </video>
              )}
            </section>
          )
        )}
        <section aria-label="来源上传记录">
          <h2 className="mb-2 font-semibold">来源上传记录</h2>
          <ServerTable
            rows={h?.items || []}
            columns={hc}
            loading={attempts.isPending}
            fetching={attempts.isFetching}
            error={attempts.error}
            retry={() => void attempts.refetch()}
            filtered={false}
            emptyTitle="尚未开始平台上传"
          />
          <Pager
            paging={attemptPage}
            nextCursor={h?.next_cursor}
            busy={attempts.isFetching}
          />
        </section>
        <section aria-label="账户资产">
          <h2 className="mb-2 font-semibold">账户资产</h2>
          <ServerTable
            rows={a?.items || []}
            columns={ac}
            loading={assets.isPending}
            fetching={assets.isFetching}
            error={assets.error}
            retry={() => void assets.refetch()}
            filtered={false}
            emptyTitle="尚无账户资产"
          />
          <Pager
            paging={assetPage}
            nextCursor={a?.next_cursor}
            busy={assets.isFetching}
          />
        </section>
      </div>
    </ManagementSheet>
  )
}
