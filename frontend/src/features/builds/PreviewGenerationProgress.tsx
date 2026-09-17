import { useEffect, useState } from "react"
import type { PreviewSummary } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Spinner } from "@/components/ui/spinner"
import { isForbidden, RequestError } from "@/features/tenants/shared"

const phaseLabels = {
  inputs: "正在校验输入与账户",
  dramas: "正在整理剧目与素材",
  units: "正在检查剧目与账户组合",
  digest: "正在汇总并冻结预览",
  complete: "正在确认冻结结果",
}

function durationLabel(milliseconds: number) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000))
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

export function PreviewGenerationProgress({
  preview,
  error,
  refreshing,
  onRefresh,
}: {
  preview: PreviewSummary
  error: unknown
  refreshing: boolean
  onRefresh: () => void
}) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  const progress = preview.generation_progress
  const elapsed = now - Date.parse(preview.created_at)
  const sinceProgress = now - Date.parse(progress.updated_at)
  const stalled = sinceProgress >= 30_000
  const total = progress.total_units
  // 百分比只反映真实组合检查进度；组合检查结束后仍需汇总冻结，不伪造整体完成度。
  const percentage =
    total !== null && total > 0 && progress.completed_units < total
      ? (progress.completed_units / total) * 100
      : null

  return (
    <Card aria-label="预览生成进度" className="min-w-0">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Spinner aria-label="正在生成搭建预览" />
          正在生成搭建预览
        </CardTitle>
        <CardDescription>
          请保持当前预览；生成完成后将自动展示可提交范围。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p aria-live="polite" className="text-sm">
            {phaseLabels[progress.phase]}
          </p>
          <p className="text-sm text-muted-foreground">
            已用时 {durationLabel(elapsed)}
          </p>
        </div>
        <div className="flex flex-col gap-2">
          <p className="text-sm tabular-nums">
            {total === null
              ? `已处理 ${progress.completed_units} 个组合 · 总数待确认`
              : `${progress.completed_units} / ${total} 个组合`}
          </p>
          {percentage !== null && (
            <Progress
              value={percentage}
              aria-label="剧目与账户组合检查进度"
              aria-valuetext={`已处理 ${progress.completed_units} 个，共 ${total} 个组合`}
            />
          )}
          <p className="text-xs text-muted-foreground">
            已处理数量包含阻断组合；最终提交数量与预算将在冻结完成后展示。
          </p>
        </div>
        {isForbidden(error) ? (
          <RequestError error={error} />
        ) : (
          (!!error || stalled) && (
            <Alert>
              <AlertTitle>
                {error ? "暂时无法获取最新进度" : "进度暂未更新"}
              </AlertTitle>
              <AlertDescription>
                <p>
                  {error
                    ? "当前显示最后一次获取的进度，请检查网络或重新查询。后台任务可能仍在运行。"
                    : `已有 ${durationLabel(sinceProgress)} 未收到新进度，任务可能仍在处理。可重新查询当前预览，持续无变化时请联系管理员。`}
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={refreshing}
                  onClick={onRefresh}
                >
                  {refreshing && <Spinner data-icon="inline-start" />}
                  重新查询进度
                </Button>
              </AlertDescription>
            </Alert>
          )
        )}
      </CardContent>
    </Card>
  )
}
