import { useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useRef, useState } from "react"
import {
  type AccountAsset,
  MaterialsService,
  type RemoteMaterialPreview,
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

type Preview =
  | { kind: "original"; url: string }
  | ({ kind: "remote" } & RemoteMaterialPreview)

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
  const queryClient = useQueryClient()
  const [sessionToken] = useState(() => localStorage.getItem("access_token"))
  const [previewAuthorized, setPreviewAuthorized] = useState(true)
  const [preview, setPreview] = useState<Preview | null>(null),
    [previewError, setPreviewError] = useState(false),
    [previewLoading, setPreviewLoading] = useState(false),
    previewController = useRef<AbortController | null>(null)
  useEffect(() => {
    let revoked = false
    const checkSession = () => {
      if (!revoked && sessionToken !== localStorage.getItem("access_token")) {
        revoked = true
        previewController.current?.abort()
        setPreview(null)
        setPreviewError(false)
        setPreviewLoading(false)
        setPreviewAuthorized(false)
      }
    }
    const unsubscribe = queryClient.getQueryCache().subscribe(checkSession)
    window.addEventListener("storage", checkSession)
    checkSession()
    return () => {
      unsubscribe()
      window.removeEventListener("storage", checkSession)
      previewController.current?.abort()
    }
  }, [queryClient, sessionToken])
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
  async function loadPreview(kind: "original" | "remote") {
    if (
      !previewAuthorized ||
      !sessionToken ||
      sessionToken !== localStorage.getItem("access_token")
    )
      return
    previewController.current?.abort()
    const controller = new AbortController()
    previewController.current = controller
    setPreviewLoading(true)
    setPreviewError(false)
    setPreview(null)
    try {
      const request = {
        path,
        query: { bc_id: bcId },
        signal: controller.signal,
      }
      const result: Preview =
        kind === "remote"
          ? {
              kind,
              ...(await MaterialsService.readRemotePreview(request)).data,
            }
          : {
              kind,
              url: (await MaterialsService.readOriginalPreview(request)).data
                .url,
            }
      if (
        !controller.signal.aborted &&
        sessionToken === localStorage.getItem("access_token")
      )
        setPreview(result)
    } catch (error) {
      if (!controller.signal.aborted) {
        if (error instanceof Error) handleApiError(error)
        if (isForbidden(error)) onForbidden()
        setPreviewError(true)
      }
    } finally {
      if (!controller.signal.aborted) setPreviewLoading(false)
    }
  }
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
                  ? "暂存原件当前可读取"
                  : "暂无可读取的暂存原件"}
              </p>
              <Stage status={detail.data.status} />
              <div className="flex flex-wrap gap-2">
                {detail.data.available_account_count > 0 && (
                  <Button
                    variant="outline"
                    disabled={previewLoading || !previewAuthorized}
                    onClick={() => void loadPreview("remote")}
                  >
                    {previewLoading ? "正在读取预览…" : "预览账户素材"}
                  </Button>
                )}
                {detail.data.original_available && (
                  <Button
                    variant="outline"
                    disabled={previewLoading || !previewAuthorized}
                    onClick={() => void loadPreview("original")}
                  >
                    {previewLoading ? "正在读取预览…" : "预览原文件"}
                  </Button>
                )}
              </div>
              {preview?.kind === "remote" && (
                <div className="flex flex-col gap-1 text-sm">
                  <span>实际预览账户</span>
                  <CopyValue value={preview.advertiser_id} />
                  <span>实际预览 VID</span>
                  <CopyValue value={preview.video_id} />
                  <span className="text-muted-foreground">
                    {preview.width} × {preview.height} · {preview.duration} 秒 ·{" "}
                    {preview.format}
                  </span>
                </div>
              )}
              {previewError && (
                <Alert variant="destructive">
                  <AlertDescription>
                    暂无法预览，请稍后重新获取。
                  </AlertDescription>
                </Alert>
              )}
              {preview && (
                <video
                  aria-label={
                    preview.kind === "remote"
                      ? "账户素材视频预览"
                      : "原文件视频预览"
                  }
                  src={preview.url}
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
