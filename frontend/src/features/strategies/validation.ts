import type { StrategyConfig_Output } from "@/client"
export const DEFAULT_SUFFIX = "-{YYYYMMDD}-{batch_short_id}"
export function suffixError(value: string): string | undefined {
  if (Array.from(value).length > 1000) return "后缀不能超过 1000 个字符。"
  if (
    Array.from(value).some(
      (char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127,
    )
  )
    return "后缀不能包含控制字符。"
  let batch = false
  for (let i = 0; i < value.length; i++) {
    if (value[i] === "{" && value[i + 1] === "{") {
      i++
      continue
    }
    if (value[i] === "}" && value[i + 1] === "}") {
      i++
      continue
    }
    if (value[i] === "}") return "后缀括号不完整。"
    if (value[i] !== "{") continue
    const end = value.indexOf("}", i + 1),
      field = value.slice(i + 1, end)
    if (end < 0 || !["YYYYMMDD", "batch_short_id"].includes(field))
      return "仅允许 {YYYYMMDD} 和 {batch_short_id} 变量。"
    if (field === "batch_short_id") batch = true
    i = end
  }
  if (!batch) return "后缀必须包含 {batch_short_id}，用于区分批次。"
}
export function renderSuffix(value: string) {
  if (suffixError(value)) return null
  return value.replace(
    /\{\{|\}\}|\{YYYYMMDD\}|\{batch_short_id\}/g,
    (token) =>
      ({
        "{{": "{",
        "}}": "}",
        "{YYYYMMDD}": "20260908",
        "{batch_short_id}": "B7K2M9Q4",
      })[token]!,
  )
}
export function normalizeDecimal(value: string) {
  const [whole, fraction = ""] = value.split(".")
  return `${whole.replace(/^0+(?=\d)/, "") || "0"}${fraction.replace(/0+$/, "") ? `.${fraction.replace(/0+$/, "")}` : ""}`
}
export function decimalError(value: string) {
  if (!/^\d+(\.\d+)?$/.test(value) || !/[1-9]/.test(value))
    return "请输入大于 0 的十进制数。"
  const normalized = normalizeDecimal(value),
    [whole, fraction = ""] = normalized.split(".")
  if (
    fraction.length > 12 ||
    whole.replace(/^0+/, "").length > 26 ||
    whole.replace(/^0+/, "").length + fraction.length > 38
  )
    return "最多 26 位整数、12 位小数。"
}
export const configFingerprint = (config: StrategyConfig_Output) =>
  JSON.stringify({
    currency: config.currency,
    group_size: config.group_size,
    creative_count: config.creative_count,
    copy_pool_version: config.copy_pool_version,
    budget: normalizeDecimal(config.budget),
    target_roas: normalizeDecimal(config.target_roas),
    cta_option_ids: config.cta_option_ids || [],
    campaign_suffix: config.campaign_suffix ?? DEFAULT_SUFFIX,
  })
export const issueMessages: Record<string, string> = {
  copy_pool_exhausted: "创意数量超过有效且不重复的英文文案数。",
  copy_pool_not_found: "引用的文案池版本不可用。",
  invalid_name_template: "后缀模板无效，请检查变量和括号。",
  invalid_cta_options: "已有 CTA 配置无效，请联系管理员核实。",
  invalid_strategy_name: "请输入有效策略名称。",
  version_conflict: "服务器版本已变化，本地输入已保留。",
  idempotency_conflict: "保存请求身份与内容不一致，请核实原保存结果。",
}
