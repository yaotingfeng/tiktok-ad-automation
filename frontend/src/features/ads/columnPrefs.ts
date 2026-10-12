import type { ColumnDef } from "./metricCatalog"
import type { AdsDimension } from "./search"

export type ColumnPrefs = { hidden: string[]; order: string[] }

// 默认隐藏的列：revenue_d3, revenue_d7, revenue_d14, revenue_d30
export const DEFAULT_HIDDEN = [
  "revenue_d3",
  "revenue_d7",
  "revenue_d14",
  "revenue_d30",
]

const keyOf = (scope: string, dimension: AdsDimension) =>
  `ads-columns:${scope}:${dimension}`

// 列偏好只是个人浏览习惯，放本地即可；存储不可用时退回字典默认列，不影响报表。
export function loadColumnPrefs(
  scope: string,
  dimension: AdsDimension,
): ColumnPrefs | null {
  try {
    const raw = localStorage.getItem(keyOf(scope, dimension))
    return raw ? (JSON.parse(raw) as ColumnPrefs) : null
  } catch {
    return null
  }
}

export function saveColumnPrefs(
  scope: string,
  dimension: AdsDimension,
  prefs: ColumnPrefs,
) {
  try {
    localStorage.setItem(keyOf(scope, dimension), JSON.stringify(prefs))
  } catch {
    // 隐私模式或存储已满时放弃保存。
  }
}

export function applyColumnPrefs(
  columns: ColumnDef[],
  prefs: ColumnPrefs | null,
): ColumnDef[] {
  // 无偏好时应用默认隐藏规则
  if (!prefs) {
    return columns.filter((column) => !DEFAULT_HIDDEN.includes(column.key))
  }
  const rank = new Map(prefs.order.map((key, index) => [key, index]))
  return [...columns]
    .sort(
      (a, b) =>
        (rank.get(a.key) ?? 1e6 + columns.indexOf(a)) -
        (rank.get(b.key) ?? 1e6 + columns.indexOf(b)),
    )
    .filter((column) => !prefs.hidden.includes(column.key))
}
