type Tone = "default" | "secondary" | "destructive" | "outline"

// 主状态优先看审核与投放（secondary_status），再看开关（operation_status）；
// 未登记的枚举原样显示，不隐藏，避免平台新增状态时投手看不到。
const SECONDARY: Array<[RegExp, string, Tone]> = [
  [/DELETE/, "已删除", "outline"],
  [/AUDIT_DENY|REJECT|NOT_APPROVE/, "未通过", "destructive"],
  [/AUDIT|REVIEW/, "审核中", "secondary"],
  [/BUDGET_EXCEED|OFFLINE_BUDGET|BALANCE/, "预算不足", "destructive"],
  [/NOT_START|NO_SCHEDULE|TIME_NO_REACH|DONE/, "不在排期", "secondary"],
  [/DISABLE/, "已暂停", "outline"],
  [/ENABLE|DELIVERY_OK|DELIVERING/, "投放中", "default"],
]
const OPERATION: Record<string, [string, Tone]> = {
  ENABLE: ["投放中", "default"],
  DISABLE: ["已暂停", "outline"],
  DELETE: ["已删除", "outline"],
}
const ACCOUNT: Record<string, [string, Tone]> = {
  STATUS_ENABLE: ["正常", "default"],
  STATUS_DISABLE: ["已停用", "outline"],
  STATUS_PENDING_CONFIRM: ["审核中", "secondary"],
  STATUS_CONFIRM_FAIL: ["未通过", "destructive"],
  STATUS_LIMIT: ["受限", "destructive"],
  ENABLE: ["正常", "default"],
  DISABLE: ["已停用", "outline"],
  UNKNOWN: ["未知", "outline"],
}

export function statusLabel(
  display: Record<string, string | null | undefined>,
) {
  const account = display.account_status
  if (account) {
    const [label, tone] = ACCOUNT[account] ?? [account, "outline" as Tone]
    return { label, tone, raw: account }
  }
  const secondary = display.secondary_status ?? ""
  const operation = display.operation_status ?? ""
  const raw = [operation, secondary, display.review_status]
    .filter(Boolean)
    .join(" / ")
  if (operation === "DISABLE")
    return { label: "已暂停", tone: "outline" as Tone, raw }
  for (const [pattern, label, tone] of SECONDARY)
    if (secondary && pattern.test(secondary)) return { label, tone, raw }
  if (operation) {
    const [label, tone] = OPERATION[operation] ?? [operation, "outline" as Tone]
    return { label, tone, raw }
  }
  return { label: "未知", tone: "outline" as Tone, raw: raw || "—" }
}
