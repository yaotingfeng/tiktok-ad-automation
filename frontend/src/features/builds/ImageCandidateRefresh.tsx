import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import type { CandidateProgress } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { RequestError } from "@/features/tenants/shared"
import { change, imageApi, imagePath } from "./imageCompositionApi"
export function ImageCandidateRefresh({
  tenantId,
  draftId,
  revision,
  progress,
  disabled,
  onChanged,
}: {
  tenantId: string
  draftId: string
  revision: number
  progress: CandidateProgress | null
  disabled: boolean
  onChanged: () => void
}) {
  const [error, setError] = useState<unknown>(),
    [busy, setBusy] = useState(false)
  const query = useQuery({
    queryKey: ["candidate-progress", progress?.snapshot_id],
    enabled: progress?.status === "PENDING",
    queryFn: async ({ signal }) =>
      (
        await imageApi.candidateProgress({
          path: {
            ...imagePath(tenantId, draftId),
            snapshot_id: progress!.snapshot_id,
          },
          signal,
        })
      ).data,
    refetchInterval: (q) => (q.state.data?.status === "PENDING" ? 1500 : false),
  })
  // 取消/采纳后的草稿状态优先，不能让旧轮询缓存重新显示待处理按钮。
  const current =
    progress?.status === "PENDING" ? (query.data ?? progress) : progress
  async function act(action: "refresh" | "adopt" | "cancel") {
    setBusy(true)
    try {
      const body = change(revision),
        path = imagePath(tenantId, draftId)
      if (action === "refresh") await imageApi.refresh({ path, body })
      else if (action === "adopt")
        await imageApi.adopt({
          path,
          body: { ...body, snapshot_id: current!.snapshot_id },
        })
      else
        await imageApi.cancelCandidates({
          path: { ...path, snapshot_id: current!.snapshot_id },
          body,
        })
      onChanged()
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex flex-col gap-2">
      <Button
        variant="outline"
        disabled={disabled || busy || current?.status === "PENDING"}
        onClick={() => void act("refresh")}
      >
        刷新匹配图片
      </Button>
      {!!error && <RequestError error={error} />}{" "}
      {current && (
        <Alert>
          <AlertDescription>
            {current.status === "PENDING"
              ? `正在匹配图片：已处理 ${current.processed} 张`
              : current.status === "READY"
                ? `新候选新增 ${current.added ?? 0} 张，移除 ${current.removed ?? 0} 张。采纳后仍需查看生成结果并应用。`
                : "图片匹配已停止，可重新刷新。"}
            {current.status === "READY" && (
              <Button
                disabled={disabled || busy}
                variant="outline"
                onClick={() => void act("adopt")}
              >
                采纳候选变化
              </Button>
            )}
            {current.status === "PENDING" && (
              <Button
                disabled={disabled || busy}
                variant="outline"
                onClick={() => void act("cancel")}
              >
                取消匹配
              </Button>
            )}
          </AlertDescription>
        </Alert>
      )}
    </div>
  )
}
