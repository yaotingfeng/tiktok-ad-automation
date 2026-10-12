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

  // 已知列键集合（用户曾保存过的列）
  const knownKeys = new Set(prefs.order)

  // 为每列分配排序优先级：已知列按 order 排序，新列按字典位置插入
  const rank = new Map<string, number>()
  let nextRank = 0
  for (let i = 0; i < columns.length; i++) {
    const key = columns[i].key
    if (knownKeys.has(key)) {
      // 已知列：使用 prefs.order 中的位置
      rank.set(key, prefs.order.indexOf(key))
    } else {
      // 新列：按字典中的相对位置插入，排在所有已知列之后但保持相对顺序
      // 为了实现插入到字典位置，我们需要找到字典中前一个已知列的位置
      let insertAfter = -1
      for (let j = i - 1; j >= 0; j--) {
        if (knownKeys.has(columns[j].key)) {
          insertAfter = prefs.order.indexOf(columns[j].key)
          break
        }
      }
      // 新列插入到 insertAfter 之后，使用小数排序确保相对顺序
      if (insertAfter === -1) {
        // 前面没有已知列，插入到开头
        rank.set(key, -1000 + nextRank * 0.001)
      } else {
        rank.set(key, insertAfter + 0.001 + nextRank * 0.0001)
      }
      nextRank++
    }
  }

  return [...columns]
    .filter((column) => {
      // 忽略字典中不存在的旧键（已在 columns 中过滤）
      if (knownKeys.has(column.key)) {
        // 已知列：使用用户明确的隐藏列表
        return !prefs.hidden.includes(column.key)
      }
      // 新列：默认显示，除非在 DEFAULT_HIDDEN 中
      return !DEFAULT_HIDDEN.includes(column.key)
    })
    .sort((a, b) => (rank.get(a.key) ?? 0) - (rank.get(b.key) ?? 0))
}
