import type { StrategyConfig_Output } from "@/client"
export const DEFAULT_NAME_TEMPLATE = "{provider_drama}-{drama_id}"
export const NAME_LABELS: Record<string, string> = {
  provider_drama: "版权方＋剧名",
  drama_id: "剧目 ID",
  YYYYMMDD: "日期",
}
const NAME_FIELDS = Object.keys(NAME_LABELS)

// 编辑界面使用中文字段，API 只保存规范变量；字面花括号保持原样。
export function displayNameTemplate(value: string) {
  return value.replace(/\{\{|\}\}|\{([^{}]+)\}/g, (token, field: string) =>
    NAME_FIELDS.includes(field) ? `[${NAME_LABELS[field]}]` : token,
  )
}
export function parseNameTemplate(value: string) {
  return value
    .replace(/\[([^[\]]*)\]/g, (_, label: string) => {
      const field = NAME_FIELDS.find((key) => NAME_LABELS[key] === label)
      return `{${field || label}}`
    })
    .replace(/\[/g, "{")
    .replace(/\]/g, "}")
}
function templateError(
  value: string,
  allowed: string[],
  required: string[],
): string | undefined {
  if (!value.trim()) return "请填写命名模板。"
  if (Array.from(value).length > 1000) return "模板不能超过 1000 个字符。"
  if (
    Array.from(value).some(
      (char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127,
    )
  )
    return "模板不能包含控制字符。"
  const fields = new Set<string>()
  for (let i = 0; i < value.length; i++) {
    if (
      (value[i] === "{" && value[i + 1] === "{") ||
      (value[i] === "}" && value[i + 1] === "}")
    ) {
      i++
      continue
    }
    if (value[i] === "}") return "模板括号不完整。"
    if (value[i] !== "{") continue
    const end = value.indexOf("}", i + 1),
      field = value.slice(i + 1, end)
    if (end < 0 || !allowed.includes(field))
      return "请使用版权方＋剧名、剧目 ID、日期字段；批次编号由系统自动添加。"
    if (fields.has(field)) return `${NAME_LABELS[field]}只能出现一次。`
    fields.add(field)
    i = end
  }
  const missing = required.filter((field) => !fields.has(field))
  if (missing.length)
    return `请保留${missing.map((name) => NAME_LABELS[name]).join("、")}字段。`
}
export function nameTemplateError(value: string) {
  return (
    templateError(value, NAME_FIELDS, ["provider_drama", "drama_id"]) ||
    (!value.startsWith("{provider_drama}")
      ? "版权方＋剧名须保留在开头，以保持归因名称完整。"
      : undefined)
  )
}
export function renderNameExample(
  value: string,
  providerDrama = "jiashu-The Bond",
  dramaId = "106001",
) {
  if (nameTemplateError(value)) return null
  const values: Record<string, string> = {
    "{{": "{",
    "}}": "}",
    "{provider_drama}": providerDrama,
    "{drama_id}": dramaId,
    "{YYYYMMDD}": "20260913",
  }
  return `${value.replace(/\{\{|\}\}|\{[^{}]+\}/g, (token) => values[token]!)}-A7K2`
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
    campaign_name_template:
      config.campaign_name_template ?? DEFAULT_NAME_TEMPLATE,
  })
export const issueMessages: Record<string, string> = {
  copy_pool_exhausted: "创意数量超过有效且不重复的英文文案数。",
  copy_pool_not_found: "引用的文案池版本不可用。",
  invalid_name_template: "广告名称格式无效，请检查字段和括号。",
  invalid_cta_options: "已有 CTA 配置无效，请联系管理员核实。",
  invalid_strategy_name: "请输入有效策略名称。",
  version_conflict: "服务器版本已变化，本地输入已保留。",
  idempotency_conflict: "保存请求身份与内容不一致，请核实原保存结果。",
}
