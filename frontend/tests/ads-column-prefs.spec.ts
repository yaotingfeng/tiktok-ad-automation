import { expect, test } from "@playwright/test"
import { applyColumnPrefs } from "../src/features/ads/columnPrefs"
import type { ColumnDef } from "../src/features/ads/metricCatalog"

const mockColumns: ColumnDef[] = [
  {
    key: "spend",
    label: "消耗",
    fullName: "消耗",
    unit: "money",
    recoveryDay: null,
  },
  {
    key: "roas_d0",
    label: "ROAS",
    fullName: "当日ROAS",
    unit: "ratio",
    recoveryDay: 0,
  },
  {
    key: "revenue_d3",
    label: "D3收益",
    fullName: "第3天收益",
    unit: "money",
    recoveryDay: 2,
  },
  {
    key: "impressions",
    label: "展示",
    fullName: "展示量",
    unit: "count",
    recoveryDay: null,
  },
  {
    key: "cpm",
    label: "CPM",
    fullName: "千次展示成本",
    unit: "money",
    recoveryDay: null,
  },
]

test("无偏好时应用 DEFAULT_HIDDEN", () => {
  const result = applyColumnPrefs(mockColumns, null)
  expect(result.map((c) => c.key)).toEqual([
    "spend",
    "roas_d0",
    "impressions",
    "cpm",
  ])
  expect(result.find((c) => c.key === "revenue_d3")).toBeUndefined()
})

test("新列插入到字典位置", () => {
  // 用户偏好中只有 spend 和 impressions，roas_d0/cpm 是新列
  const prefs = {
    hidden: [],
    order: ["spend", "impressions"],
  }
  const result = applyColumnPrefs(mockColumns, prefs)
  // 新列 roas_d0 应插入到 spend 之后（字典位置），cpm 在 impressions 之后
  expect(result.map((c) => c.key)).toEqual([
    "spend",
    "roas_d0",
    "impressions",
    "cpm",
  ])
})

test("新列在 DEFAULT_HIDDEN 中保持隐藏", () => {
  // 用户偏好中只有 spend 和 roas_d0，revenue_d3 是新列且在 DEFAULT_HIDDEN 中
  const prefs = {
    hidden: [],
    order: ["spend", "roas_d0"],
  }
  const result = applyColumnPrefs(mockColumns, prefs)
  // revenue_d3 应该被隐藏
  expect(result.find((c) => c.key === "revenue_d3")).toBeUndefined()
  expect(result.map((c) => c.key)).toContain("spend")
  expect(result.map((c) => c.key)).toContain("roas_d0")
})

test("字典中不存在的旧键被忽略", () => {
  const prefs = {
    hidden: [],
    order: ["spend", "old_metric", "roas_d0", "stale_key"],
  }
  const result = applyColumnPrefs(mockColumns, prefs)
  // old_metric 和 stale_key 不在 mockColumns 中，应被忽略
  expect(result.map((c) => c.key)).not.toContain("old_metric")
  expect(result.map((c) => c.key)).not.toContain("stale_key")
  expect(result.map((c) => c.key)).toContain("spend")
  expect(result.map((c) => c.key)).toContain("roas_d0")
})

test("已知列使用用户明确的隐藏列表", () => {
  const prefs = {
    hidden: ["roas_d0"],
    order: ["spend", "roas_d0", "impressions"],
  }
  const result = applyColumnPrefs(mockColumns, prefs)
  expect(result.find((c) => c.key === "roas_d0")).toBeUndefined()
  expect(result.map((c) => c.key)).toContain("spend")
  expect(result.map((c) => c.key)).toContain("impressions")
})
