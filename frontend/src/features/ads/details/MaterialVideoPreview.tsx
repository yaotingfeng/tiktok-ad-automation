import { useMutation } from "@tanstack/react-query"
import { AxiosError } from "axios"
import { MaterialsService, type ReportRow } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { useTenantScope } from "@/features/tenants/TenantScope"

// 后端一次最多接受 20 个候选账户，只为在其中挑一个有读授权的账户。
const MAX_CANDIDATES = 20

/**
 * 素材维度的成片预览。素材可能是在 TikTok 后台直接上传、不在素材库里的外部视频，
 * 因此按“使用位置所在账户 + 平台视频 ID”直接读取 TikTok 预览地址。点击才读取：
 * 每次读取都会占用平台读取额度，打开详情不应自动调用。
 */
export function MaterialVideoPreview({ row }: { row: ReportRow }) {
  const { tenantId, bc } = useTenantScope()
  const bcId = bc?.bc_id
  const uses = row.material_uses ?? []
  // 视频行的使用位置都是同一个视频；帖子行取它对照上的视频。
  const videoId = uses[0]?.platform_material_id ?? null
  const advertiserIds = [
    ...new Set(
      uses
        .filter((use) => use.platform_material_id === videoId)
        .map((use) => use.ad_ref.advertiser_id),
    ),
  ].slice(0, MAX_CANDIDATES)
  const preview = useMutation({
    mutationFn: async () =>
      (
        await MaterialsService.readAccountVideoPreview({
          path: { tenant_id: tenantId! },
          query: {
            bc_id: bcId!,
            video_id: videoId!,
            advertiser_ids: advertiserIds.join(","),
          },
        })
      ).data,
  })
  const ready = Boolean(tenantId && bcId && videoId && advertiserIds.length)
  return (
    <section className="flex flex-col gap-2">
      <h3 className="font-medium">成片</h3>
      {!ready ? (
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
              disabled={preview.isPending}
              onClick={() => preview.mutate()}
            >
              {preview.isPending
                ? "正在读取预览…"
                : preview.data
                  ? "重新读取"
                  : "预览成片"}
            </Button>
          </div>
          {preview.isError && (
            <Alert variant="destructive">
              <AlertDescription>
                {errorText(preview.error) ?? "暂无法预览，请稍后重新读取。"}
              </AlertDescription>
            </Alert>
          )}
          {preview.data && (
            <>
              <p className="text-xs text-muted-foreground">
                来源账户 {preview.data.advertiser_id} · {preview.data.width} ×{" "}
                {preview.data.height} · {preview.data.duration} 秒 ·{" "}
                {preview.data.format}
              </p>
              <video
                aria-label="素材成片预览"
                src={preview.data.url}
                controls
                preload="metadata"
                className="max-h-96 w-full rounded-lg bg-black"
                // 平台预览地址有时效，失效后提示重新读取。
                onError={() => preview.reset()}
              >
                <track kind="captions" />
              </video>
            </>
          )}
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
