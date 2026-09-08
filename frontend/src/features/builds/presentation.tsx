import { useBlocker } from "@tanstack/react-router"
import { AxiosError } from "axios"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { handleApiError } from "@/lib/api-feedback"
export function BuildGuard({
  dirty,
  title = "有未保存的修改",
  description = "离开会丢弃本页未保存输入，服务器中已保存的草稿和任务会保留。",
  leaveLabel = "丢弃未保存修改",
}: {
  dirty: boolean
  title?: string
  description?: string
  leaveLabel?: string
}) {
  const blocker = useBlocker({
    shouldBlockFn: () => dirty,
    enableBeforeUnload: dirty,
    withResolver: true,
  })
  return (
    <Dialog
      open={blocker.status === "blocked"}
      onOpenChange={(open) => {
        if (!open) blocker.reset?.()
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => blocker.reset?.()}>
            留在当前页
          </Button>
          <Button onClick={() => blocker.proceed?.()}>{leaveLabel}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
export const states: Record<string, string> = {
  DRAFT: "草稿",
  PREPARING: "准备中",
  READY: "已就绪",
  BLOCKED: "无法搭建",
  BUILDING: "正在生成",
  FROZEN: "已冻结",
  OBSOLETE: "已过期",
  FAILED: "失败",
  ready: "已就绪",
  pending: "处理中",
  needs_resolution: "待选择候选",
  result_unknown: "结果待核实",
  config_conflict: "配置冲突",
  blocked_auth: "连接需要重新认证",
  duplicate: "重复输入",
  resolved: "已解析",
  unresolved: "未解析",
  failed: "失败",
  matched: "已匹配",
  empty: "空输入",
  manual: "已手动调整",
  ambiguous: "名称存在歧义",
  not_found: "未找到匹配",
  invalid: "输入无效",
}
export function BuildStatus({ value }: { value: string }) {
  return <span>{states[value] || value}</span>
}
export function BuildError({ error }: { error: unknown }) {
  const status =
    error instanceof AxiosError ? error.response?.status : undefined
  const code =
    error instanceof AxiosError ? error.response?.data?.code : undefined
  return (
    <Alert variant="destructive">
      <AlertTitle>
        {status === 403
          ? "权限不足"
          : status === 409
            ? "草稿已更新"
            : "操作未完成"}
      </AlertTitle>
      <AlertDescription>
        {status === 403
          ? "当前角色无权执行此操作，请联系管理员。"
          : status === 409
            ? "本地输入已保留。请先查看最新版本，核对差异后重新应用修改。"
            : code === "request_not_found"
              ? "尚未查到原请求，不能据此重新创建。请继续核实。"
              : "请求未完成，输入已保留。请核实当前状态。"}
        {typeof code === "string" && (
          <span className="block break-all text-xs">{code}</span>
        )}
      </AlertDescription>
    </Alert>
  )
}
export function reportError(error: unknown) {
  if (error instanceof Error) handleApiError(error)
}
export const unknownOutcome = (error: unknown) =>
  !(error instanceof AxiosError) ||
  !error.response ||
  error.response.status >= 500
export function BuildSteps({ step }: { step: 1 | 2 | 3 }) {
  return (
    <ol
      aria-label="搭建步骤"
      className="grid grid-cols-3 gap-2 rounded-lg border bg-card p-4 text-sm"
    >
      {["批量输入", "准备与调整", "搭建预览"].map((label, i) => (
        <li
          key={label}
          aria-current={i + 1 === step ? "step" : undefined}
          className={
            i + 1 === step
              ? "font-semibold text-primary"
              : "text-muted-foreground"
          }
        >
          {i + 1} · {label}
        </li>
      ))}
    </ol>
  )
}

const reasonLabels: Record<string, string> = {
  currency_mismatch: "账户币种与策略不一致",
  materials_missing: "未匹配到可用素材",
  material_unavailable: "素材暂不可用于此账户",
  cta_options_unavailable: "CTA 尚未通过当前场景核实",
  field_limits_unverified: "平台字段限制尚未核实",
  budget_limits_unverified: "预算限制尚未核实",
  budget_out_of_range: "预算超出平台允许范围",
  roas_out_of_range: "目标 ROAS 超出允许范围",
  roas_limits_unverified: "目标 ROAS 限制尚未核实",
  scene_unsupported: "当前账户不支持所需投放场景",
  account_access_denied: "当前账户缺少搭建权限",
  account_not_in_bc: "账户不属于当前 BC",
  account_ownership_conflict: "账户归属存在冲突",
  account_metadata_incomplete: "账户信息尚未完善",
  scene_link_unavailable: "链接或应用资产尚不可用",
  name_too_long: "生成名称超过平台长度限制",
  name_invalid: "生成名称包含不支持字符",
  duplicate_campaign_name: "Campaign 名称重复",
  copy_too_long: "冻结正文超过场景长度限制",
  creative_count_exceeded: "SP 创意条数超过场景限制",
  drama_not_found: "未找到完整剧名",
  config_conflict: "已有链接配置与本次请求冲突",
  config_unverifiable: "已有配置尚未核实",
  provider_auth_expired: "版权方认证已过期",
  result_unknown: "远端结果尚待核实",
}
export function BuildReason({ code }: { code: string | null }) {
  if (!code) return null
  return (
    <span className="block break-all text-xs text-muted-foreground">
      {reasonLabels[code] && (
        <span className="block">{reasonLabels[code]}</span>
      )}
      <span>{code}</span>
    </span>
  )
}
