import type {
  ManagementCounts,
  ManagementTaskMutation,
  ManagementTaskPublic,
} from "@/client"

export const statusLabel: Record<string, string> = {
  PREPARING: "准备中",
  READY: "待提交",
  QUEUED: "排队中",
  RUNNING: "执行中",
  SUCCEEDED: "已完成",
  PARTIAL: "部分完成",
  FAILED: "失败",
  NEEDS_REVIEW: "需要核查",
  CANCELLED: "已取消",
}

const KIND_LABEL: Record<string, string> = {
  campaign: "系列",
  adgroup: "广告组",
  ad: "广告",
  creative: "素材",
  material: "素材",
  account: "账户",
}
const FIELD_LABEL: Record<ManagementTaskMutation["field"], string> = {
  budget: "预算",
  roas: "目标 ROAS",
  status: "启停",
}

/** 动作摘要，例如「暂停」「预算 设为 500」「目标 ROAS 提高 10%」。 */
export function mutationText(mutation?: ManagementTaskMutation | null) {
  if (!mutation) return "—"
  if (mutation.field === "status")
    return mutation.value === "ENABLE" ? "启用" : "暂停"
  const field = FIELD_LABEL[mutation.field]
  const value = String(mutation.value)
  switch (mutation.mode) {
    case "set":
      return `${field} 设为 ${value}`
    case "add":
      return `${field} 增加 ${value}`
    case "subtract":
      return `${field} 减少 ${value}`
    case "increase_percent":
      return `${field} 提高 ${value}%`
    case "decrease_percent":
      return `${field} 降低 ${value}%`
  }
}

/** 按层级列出目标对象数，例如「广告组 3 · 系列 1」。 */
export function targetText(task: ManagementTaskPublic) {
  const entries = Object.entries(task.target_kinds ?? {})
  if (!entries.length) return `${task.counts?.targets ?? 0} 个对象`
  return entries
    .map(([kind, total]) => `${KIND_LABEL[kind] ?? kind} ${total}`)
    .join(" · ")
}

/** 来源：自动规则显示规则名，规则已删除时只标“自动规则”；其余为人工操作人。 */
export function sourceText(task: ManagementTaskPublic) {
  if (task.source?.kind === "rule")
    return task.source.rule_name
      ? `自动规则 · ${task.source.rule_name}`
      : "自动规则（已删除）"
  return task.actor_name ? `人工 · ${task.actor_name}` : "人工"
}

/** 结果计数：只列出非零项，未知结果单独提示需要核查。 */
export function resultParts(counts?: ManagementCounts) {
  const parts: Array<{ label: string; value: number; warn?: boolean }> = [
    { label: "成功", value: counts?.accepted ?? 0 },
    { label: "失败", value: counts?.rejected ?? 0, warn: true },
    { label: "待核查", value: counts?.unknown ?? 0, warn: true },
    { label: "未发送", value: counts?.not_sent ?? 0 },
    { label: "待执行", value: counts?.pending ?? 0 },
    { label: "不支持", value: counts?.unsupported ?? 0 },
  ]
  return parts.filter((part) => part.value > 0)
}

export function displayTime(value?: string | null) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "—"
}
