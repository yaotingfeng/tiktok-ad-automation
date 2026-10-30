import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import type { ImageRule, ImageRulePatch } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { RequestError } from "@/features/tenants/shared"
import { ImageRuleFields } from "./ImageRuleFields"
import { MusicPicker } from "./MusicPicker"
import { imageApi, imagePath, change } from "./imageCompositionApi"

export function ImageDramaBatchActions({
  tenantId,
  draftId,
  revision,
  selected,
  rule,
  disabled,
  onPatch,
  onChanged,
}: {
  tenantId: string
  draftId: string
  revision: number
  selected: Record<string, string>
  rule: ImageRule
  disabled: boolean
  onPatch: (patch: ImageRulePatch | null) => void
  onChanged: () => void
}) {
  const [dialog, setDialog] = useState<
    "rules" | "music" | "reset" | "retry" | null
  >(null)
  const [patch, setPatch] = useState<ImageRulePatch>({})
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>()
  const ids = Object.keys(selected),
    path = imagePath(tenantId, draftId)
  const progress = useQuery({
    queryKey: ["selected-music-batches", tenantId, draftId],
    queryFn: async ({ signal }) =>
      (await imageApi.musicRetryBatches({ path, signal })).data,
    refetchInterval: (q) =>
      q.state.data?.some((r) => r.status === "PENDING") ? 1500 : false,
  })
  const effective = { ...rule, ...patch } as ImageRule
  return (
    <div className="flex flex-col gap-3" aria-label="所选剧目批量操作">
      <p>
        已选择 {ids.length} 部剧（最多 100
        部，勾选可跨页保留）。规则修改仍需查看生成结果并应用，手工保护与排除保留。
      </p>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          disabled={disabled || !ids.length}
          onClick={() => {
            setPatch({})
            setDialog("rules")
          }}
        >
          修改所选规则
        </Button>
        <Button
          variant="outline"
          disabled={disabled || !ids.length}
          onClick={() => setDialog("music")}
        >
          所选剧指定音乐
        </Button>
        <Button
          variant="outline"
          disabled={disabled || !ids.length}
          onClick={() => setDialog("reset")}
        >
          所选剧恢复整批规则
        </Button>
        <Button
          variant="outline"
          disabled={disabled || !ids.length}
          onClick={() => setDialog("retry")}
        >
          重试所选剧失败配乐
        </Button>
      </div>
      {(error || progress.error) && (
        <RequestError error={error || progress.error} />
      )}
      {progress.data?.slice(0, 3).map((run) => (
        <Alert key={run.run_id}>
          <AlertDescription>
            {run.status === "PENDING"
              ? "批量重试进行中"
              : run.status === "READY"
                ? "批量重试已登记"
                : run.status === "CANCELLED"
                  ? "草稿已更新，剩余重试已停止"
                  : "批量重试失败"}{" "}
            · {run.drama_ids.length} 部剧 · 已扫描 {run.processed} · 已重试{" "}
            {run.retried} · 跳过音乐保护 {run.skipped_protected} ·
            跳过无需重试项 {run.skipped_recovered}
          </AlertDescription>
        </Alert>
      ))}
      {dialog === "music" && (
        <MusicPicker
          tenantId={tenantId}
          draftId={draftId}
          onClose={() => setDialog(null)}
          onSelect={(song) =>
            onPatch({
              music_mode: "FIXED",
              fixed_music_id: song.music_id,
              single_music_id: song.music_id,
            })
          }
        />
      )}
      {dialog && dialog !== "music" && (
        <Dialog
          open
          onOpenChange={(open) => {
            if (!open && !busy) setDialog(null)
          }}
        >
          <DialogContent className="max-h-[90vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>
                {dialog === "rules"
                  ? "修改所选剧目规则"
                  : dialog === "reset"
                    ? "恢复所选剧目规则"
                    : "重试所选剧目失败配乐"}
              </DialogTitle>
              <DialogDescription>
                {Object.values(selected).join("、")}。
                {dialog === "retry"
                  ? "重试失败配乐，并恢复准备失败后尚未启动的配乐；保留手工保护和正在处理的任务。后续修改草稿会停止尚未扫描部分。"
                  : "仅改变所选剧的字段覆盖；其他剧、手工保护和候选排除保持原样。"}
              </DialogDescription>
            </DialogHeader>
            {dialog === "rules" && (
              <ImageRuleFields
                tenantId={tenantId}
                draftId={draftId}
                value={effective}
                disabled={busy}
                onChange={(next) => {
                  const changed = { ...patch }
                  for (const key of Object.keys(next) as (keyof ImageRule)[])
                    if (next[key] !== effective[key])
                      Object.assign(changed, { [key]: next[key] })
                  setPatch(changed)
                }}
              />
            )}
            {!!error && <RequestError error={error} />}
            <DialogFooter>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => setDialog(null)}
              >
                取消
              </Button>
              <Button
                disabled={busy}
                onClick={async () => {
                  if (dialog !== "retry") {
                    onPatch(dialog === "reset" ? null : patch)
                    setDialog(null)
                    return
                  }
                  setBusy(true)
                  try {
                    await imageApi.retrySelectedMusic({
                      path,
                      body: { ...change(revision), drama_ids: ids },
                    })
                    onChanged()
                    await progress.refetch()
                    setDialog(null)
                  } catch (e) {
                    setError(e)
                  } finally {
                    setBusy(false)
                  }
                }}
              >
                {dialog === "rules"
                  ? "保存所选规则"
                  : dialog === "reset"
                    ? "确认恢复所选规则"
                    : "确认重试所选失败项"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  )
}
