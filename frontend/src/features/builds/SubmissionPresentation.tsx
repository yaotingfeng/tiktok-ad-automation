import type { StepPublic, SubmissionView } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { displayTime } from "@/features/accounts/presentation"
import { ServerTable } from "@/features/tenants/shared"
export const submissionStates: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "进行中",
  NEEDS_REVIEW: "待核实",
  COMPLETED: "已完成",
  PARTIAL: "部分失败",
  FAILED: "全部失败",
}
export const stepKinds: Record<string, string> = {
  MATERIAL: "素材准备",
  CTA: "CTA 准备",
  CAMPAIGN: "Campaign",
  ADGROUP: "Ad Group",
  AD: "Ad",
  READBACK: "结果核查",
}
export const stepStates: Record<string, string> = {
  PENDING: "待处理",
  QUEUED: "排队中",
  RUNNING: "处理中",
  SUCCEEDED: "已成功",
  RETRYABLE: "可重试",
  UNKNOWN: "结果待核实",
  FAILED: "确定失败",
}
export function SubmissionBadge({ status }: { status: string }) {
  return (
    <Badge
      variant={
        status === "FAILED"
          ? "destructive"
          : status === "COMPLETED"
            ? "secondary"
            : "outline"
      }
    >
      {submissionStates[status] || stepStates[status] || status}
    </Badge>
  )
}
export function PlatformState({ step }: { step?: StepPublic | null }) {
  return (
    <div className="flex flex-col gap-1 text-xs">
      <span>
        操作状态：
        {step?.operation_status === "ENABLE"
          ? "已启用（ENABLE）"
          : step?.operation_status || "暂未获取"}
      </span>
      <span>审核/投放状态：{step?.review_status || "暂未获取"}</span>
      {step?.checked_at && (
        <span className="text-muted-foreground">
          核查于 {displayTime(step.checked_at)}
        </span>
      )}
      {step?.mismatch && (
        <span className="font-semibold">存在结果差异，需核实</span>
      )}
    </div>
  )
}
export function SubmissionProgress({ data }: { data: SubmissionView }) {
  return (
    <Card className="min-w-0">
      <CardHeader className="min-w-0">
        <CardTitle>
          <h2>广告创建结果</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-4">
        <div className="min-w-0 overflow-hidden rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>对象</TableHead>
                {[
                  "计划",
                  "提交",
                  "已创建",
                  "失败",
                  "待核实",
                  "待处理",
                  "排除",
                ].map((x) => (
                  <TableHead key={x}>{x}</TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {(
                [
                  ["campaign_count", "Campaign", "campaign"],
                  ["adgroup_count", "Ad Group", "group"],
                  ["ad_count", "Ad", "ad"],
                ] as const
              ).map(([field, label, id]) => (
                <TableRow key={field}>
                  <TableCell className="font-medium">{label}</TableCell>
                  {(
                    [
                      "planned",
                      "submitted",
                      "succeeded",
                      "failed",
                      "unknown",
                      "pending",
                      "excluded",
                    ] as const
                  ).map((key) => (
                    <TableCell key={key} data-testid={`count-${id}-${key}`}>
                      {data[key][field]}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
        <p className="text-xs text-muted-foreground">
          已创建事实与结果差异分别记录；素材、CTA
          与核查步骤不计入上述广告对象数。
        </p>
        <div className="flex min-w-0 flex-wrap gap-3 text-xs">
          {Object.entries(data.stage_counts)
            .filter(([key]) => /^(MATERIAL|CTA|READBACK):/.test(key))
            .map(([key, count]) => {
              const [kind, status] = key.split(":")
              return (
                <span key={key}>
                  {stepKinds[kind]} · {stepStates[status] || status} {count}
                </span>
              )
            })}
        </div>
      </CardContent>
    </Card>
  )
}
export function countObjects(value: {
  campaign_count?: number
  adgroup_count?: number
  ad_count?: number
}) {
  return (
    (value.campaign_count ?? 0) +
    (value.adgroup_count ?? 0) +
    (value.ad_count ?? 0)
  )
}

export function SubmissionTable<T>(
  props: Parameters<typeof ServerTable<T>>[0],
) {
  return (
    <div className="min-w-0 [&_th:first-child]:sticky [&_th:first-child]:left-0 [&_th:first-child]:bg-muted [&_td:first-child]:sticky [&_td:first-child]:left-0 [&_td:first-child]:min-w-40 [&_td:first-child]:max-w-60 [&_td:first-child]:whitespace-normal [&_td:first-child]:bg-card [&_td:first-child_span]:break-all">
      <ServerTable {...props} />
    </div>
  )
}
