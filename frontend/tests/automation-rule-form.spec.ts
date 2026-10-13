import { expect, test } from "@playwright/test"
import {
  actionLevels,
  emptyDefinition,
  minFrequency,
  summarizeAction,
  summarizeConditions,
} from "../src/features/automation-rules/ruleForm"

const catalog = {
  metrics: [
    {
      key: "spend",
      group: "delivery",
      name_zh: "消耗",
      unit: "money",
      kind: "fact",
      levels: ["campaign", "adgroup", "ad", "material"],
      aggregations: ["EACH", "DRAMA", "ALL"],
      operators: ["GT", "LT", "BETWEEN", "EQ"],
      ranged: true,
    },
    {
      key: "roas_total",
      group: "revenue",
      name_zh: "广告收益 ROAS",
      unit: "multiple",
      kind: "fact",
      levels: ["campaign", "adgroup", "ad", "material"],
      aggregations: ["EACH", "DRAMA", "ALL"],
      operators: ["GT", "LT", "BETWEEN", "EQ"],
      ranged: true,
    },
  ],
  action_levels: {
    PAUSE: ["campaign", "adgroup", "ad", "material"],
    ENABLE: ["campaign", "adgroup", "ad", "material"],
    BUDGET: ["campaign", "adgroup"],
    ROAS: ["campaign"],
  },
}

test("执行层级与后端一致", () => {
  expect(actionLevels("EACH", "adgroup", "PAUSE")).toEqual([
    "adgroup",
    "ad",
    "material",
  ])
  expect(actionLevels("DRAMA", "adgroup", "PAUSE")).toEqual([
    "campaign",
    "adgroup",
    "ad",
    "material",
  ])
  expect(actionLevels("EACH", "ad", "BUDGET")).toEqual([])
})

test("素材层级最短 1 小时", () => {
  expect(minFrequency("adgroup", "material")).toBe("1H")
  expect(minFrequency("adgroup", "adgroup")).toBe("30M")
})

test("条件与操作摘要", () => {
  const def = {
    ...emptyDefinition(),
    aggregation: "DRAMA" as const,
    conditions: [
      {
        metric: "spend",
        range: "TODAY" as const,
        operator: "GT" as const,
        values: ["100"],
      },
      {
        metric: "roas_total",
        range: "PAST_3_DAYS" as const,
        operator: "BETWEEN" as const,
        values: ["0.5", "0.8"],
      },
    ],
  }
  expect(summarizeConditions(def, catalog)).toBe(
    "每部剧合计 · 今天消耗 > 100 且 近 3 天广告收益 ROAS 介于 0.5 ~ 0.8",
  )
  expect(summarizeAction(def)).toBe("暂停 → 广告组")
})
