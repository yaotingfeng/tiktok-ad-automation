import { useMutation } from "@tanstack/react-query"
import { AxiosError } from "axios"
import { useState } from "react"
import { MaterialsService, type ReportRow } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { useTenantScope } from "@/features/tenants/TenantScope"

// 后端一次最多接受 20 个候选账户，只为在其中挑一个有读授权的账户。
const MAX_CANDIDATES = 20

export type VideoTarget = { videoId: string; advertiserIds: string[] }

/**
 * 按“账户 + 平台视频 ID”读取 TikTok 成片预览地址。素材可能是在 TikTok 后台直接
 * 上传、不在素材库里的外部视频，因此不经素材库。只在用户点击时读取：每次读取都会
 * 占用平台读取额度，打开详情不应自动调用。素材维度详情与广告详情的素材页签共用。
 */
export function useVideoPreview() {
  const { tenantId, bc } = useTenantScope()
  const bcId = bc?.bc_id
  // 预览地址已拿到但视频加载失败（多为平台地址过期），需提示用户重新读取。
  const [videoFailed, setVideoFailed] = useState(false)
  const mutation = useMutation({
    onMutate: () => setVideoFailed(false),
    mutationFn: async (target: VideoTarget) =>
      (
        await MaterialsService.readAccountVideoPreview({
          path: { tenant_id: tenantId! },
          query: {
            bc_id: bcId!,
            video_id: target.videoId,
            advertiser_ids: target.advertiserIds
              .slice(0, MAX_CANDIDATES)
              .join(","),
          },
        })
      ).data,
  })
  return {
    mutation,
    ready: Boolean(tenantId && bcId),
    videoFailed,
    onVideoError: () => setVideoFailed(true),
  }
}

/** 读取结果：错误提示、过期提示与播放器。 */
export function VideoPreviewPlayer({
  preview,
}: {
  preview: ReturnType<typeof useVideoPreview>
}) {
  const { mutation, videoFailed, onVideoError } = preview
  return (
    <>
      {mutation.isError && (
        <Alert variant="destructive">
          <AlertDescription>
            {errorText(mutation.error) ?? "暂无法预览，请稍后重新读取。"}
          </AlertDescription>
        </Alert>
      )}
      {videoFailed && (
        <Alert variant="destructive">
          <AlertDescription>
            视频加载失败，预览地址可能已过期，请重新读取。
          </AlertDescription>
        </Alert>
      )}
      {mutation.data && !videoFailed && (
        <>
          <p className="text-xs text-muted-foreground">
            来源账户 {mutation.data.advertiser_id} · {mutation.data.width} ×{" "}
            {mutation.data.height} · {mutation.data.duration} 秒 ·{" "}
            {mutation.data.format}
          </p>
          <video
            aria-label="素材成片预览"
            src={mutation.data.url}
            controls
            preload="metadata"
            className="max-h-96 w-full rounded-lg bg-black"
            // 平台预览地址有时效，失效后提示重新读取。
            onError={onVideoError}
          >
            <track kind="captions" />
          </video>
        </>
      )}
    </>
  )
}

/** 素材维度详情的成片预览；候选账户取自该素材的使用位置。 */
export function MaterialVideoPreview({ row }: { row: ReportRow }) {
  const preview = useVideoPreview()
  const uses = row.material_uses ?? []
  // 视频行的使用位置都是同一个视频；帖子行取它对照上的视频。
  const videoId = uses[0]?.platform_material_id ?? null
  const advertiserIds = [
    ...new Set(
      uses
        .filter((use) => use.platform_material_id === videoId)
        .map((use) => use.ad_ref.advertiser_id),
    ),
  ]
  const { mutation } = preview
  return (
    <section className="flex flex-col gap-2">
      <h3 className="font-medium">成片</h3>
      {!preview.ready || !videoId || !advertiserIds.length ? (
        <p className="text-sm text-muted-foreground">
          该素材暂无广告使用位置，无法读取成片
        </p>
      ) : (
        <>
          <div>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={mutation.isPending}
              onClick={() => mutation.mutate({ videoId, advertiserIds })}
            >
              {mutation.isPending
                ? "正在读取预览…"
                : mutation.data
                  ? "重新读取"
                  : "预览成片"}
            </Button>
          </div>
          <VideoPreviewPlayer preview={preview} />
        </>
      )}
    </section>
  )
}

// 后端错误体带中文说明（如“当前没有可读取该素材的广告账户授权”），直接展示给用户。
function errorText(error: unknown): string | null {
  if (!(error instanceof AxiosError)) return null
  const message = (error.response?.data as { message?: unknown } | undefined)
    ?.message
  return typeof message === "string" ? message : null
}
