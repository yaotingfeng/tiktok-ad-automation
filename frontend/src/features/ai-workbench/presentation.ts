/** 页面统一使用业务用语；接口枚举和原始 Decimal 值保持不变。 */
export function optimizationModeLabel(value?: string) {
  return value === "AUTO" ? "自动优化" : value === "MANUAL" ? "人工确认" : "—"
}

export function priorityLabel(value?: string) {
  return value === "SPEND"
    ? "消耗优先"
    : value === "ROAS"
      ? "ROAS 优先"
      : value === "BALANCED" || !value
        ? "平衡"
        : "—"
}

export function workbenchNumber(value?: string | number | null) {
  if (value === null || value === undefined || value === "") return "—"
  const number = Number(value)
  return Number.isFinite(number)
    ? new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(
        number,
      )
    : "—"
}
