type Tone = "default" | "secondary" | "destructive" | "outline"

// 主状态优先看审核与投放（secondary_status），再看开关（operation_status）；
// 未登记的枚举原样显示，不隐藏，避免平台新增状态时投手看不到。
// "投放中"只用于平台明确的投放状态（DELIVERY_OK 等）；系列的 CAMPAIGN_STATUS_ENABLE
// 和开关 ENABLE 只表示开关打开、不代表正在花钱，显示为"已开启"。
const SECONDARY: Array<[RegExp, string, Tone]> = [
  [/DELETE/, "已删除", "outline"],
  [/AUDIT_DENY|REJECT|NOT_APPROVE/, "未通过", "destructive"],
  [/AUDIT|REVIEW/, "审核中", "secondary"],
  [/BUDGET_EXCEED|OFFLINE_BUDGET|BALANCE/, "预算不足", "destructive"],
  [/NOT_START|NO_SCHEDULE|TIME_NO_REACH|DONE/, "不在排期", "secondary"],
  [/DISABLE/, "已暂停", "outline"],
  [/DELIVERY_OK|DELIVERING/, "投放中", "default"],
  [/ENABLE/, "已开启", "secondary"],
]
const OPERATION: Record<string, [string, Tone]> = {
  ENABLE: ["已开启", "secondary"],
  DISABLE: ["已暂停", "outline"],
  DELETE: ["已删除", "outline"],
}
// 账户目录同时存储带前缀与不带前缀两种形式（directory_capabilities.py 查询条件同时包含两者）
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
  // 剧行状态由后端按成员系列汇总：开启时附上开启系列数，例如"已开启 3/18"。
  if (display.campaign_count) {
    const operation = display.operation_status ?? ""
    const [label, tone] = OPERATION[operation] ?? ["未知", "outline" as Tone]
    const enabled = display.enabled_campaign_count ?? "0"
    const raw = `${enabled}/${display.campaign_count} 个系列开启`
    return {
      label:
        operation === "ENABLE"
          ? `${label} ${enabled}/${display.campaign_count}`
          : label,
      tone,
      raw,
    }
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
