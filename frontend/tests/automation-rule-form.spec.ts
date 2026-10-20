import { expect, test } from "@playwright/test"
import {
  actionLevels,
  aggregationLabel,
  aggregationOptions,
  emptyDefinition,
  fromZonedInput,
  minFrequency,
  summarizeAction,
  summarizeConditions,
  toZonedInput,
  validateDefinition,
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

test("素材按广告内位置或同一视频跨账户判断", () => {
  expect(aggregationOptions("material")).toEqual(["EACH", "VIDEO"])
  expect(aggregationOptions("ad")).toEqual(["EACH", "DRAMA", "ALL"])
  expect(aggregationLabel("EACH", "material")).toBe("每条广告内的素材")
  expect(aggregationLabel("EACH", "ad")).toBe("每个对象")
  // 视频口径只能对素材本身执行。
  expect(actionLevels("VIDEO", "material", "PAUSE")).toEqual(["material"])
})

test("恢复保持素材最短 1 小时，暂停支持 15 分钟", () => {
  expect(minFrequency("adgroup", "material", "ENABLE")).toBe("1H")
  expect(minFrequency("adgroup", "adgroup", "ENABLE")).toBe("30M")
  expect(minFrequency("material", "material", "PAUSE")).toBe("15M")
})

test("条件与操作摘要", () => {
  const def = {
    ...emptyDefinition(),
    aggregation: "DRAMA" as const,
    mode: "CONFIRM" as const,
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
  expect(summarizeAction({ ...def, mode: "NOTIFY" })).toBe("仅发送通知")
})

test("表单校验列出中文错误", () => {
  const ok = {
    ...emptyDefinition(),
    conditions: [
      {
        metric: "spend",
        range: "TODAY" as const,
        operator: "GT" as const,
        values: ["10"],
      },
    ],
  }
  expect(validateDefinition(ok, catalog)).toEqual([])
  const bad = {
    ...ok,
    accounts: { mode: "BC" as const, bc_ids: [], advertiser_ids: [] },
    conditions: [
      {
        metric: "spend",
        range: "TODAY" as const,
        operator: "GT" as const,
        values: [""],
      },
      {
        metric: "roas_total",
        range: "TODAY" as const,
        operator: "BETWEEN" as const,
        values: ["2", "1"],
      },
    ],
  }
  expect(validateDefinition(bad, catalog)).toEqual([
    "按 BC 选择时至少选择一个 BC",
    "条件 1 需要填写数值",
    "条件 2 的下限必须小于上限",
  ])
  const budget = {
    ...ok,
    action: {
      type: "BUDGET" as const,
      level: "adgroup" as const,
      adjustment: {
        mode: "INCREASE" as const,
        value_type: "PERCENT" as const,
        value: "0",
        limit: "",
      },
      frequency: {
        type: "CUSTOM" as const,
        window: null,
        unit: "HOUR" as const,
        count: null,
      },
    },
  }
  expect(validateDefinition(budget, catalog)).toEqual([
    "调整数值需大于 0",
    "提高预算需要设置上限",
    "自定义频控需要填写时间窗口和次数",
  ])
  const material = {
    ...ok,
    apply_to: { ...ok.apply_to, level: "material" as const },
    action: { type: "ENABLE" as const, level: "material" as const },
    schedule: { ...ok.schedule, frequency: "30M" as const },
  }
  expect(validateDefinition(material, catalog)).toEqual([
    "当前操作最短每 1 小时运行一次",
  ])
  const once = {
    ...ok,
    schedule: { ...ok.schedule, window: "ONCE" as const, once_at: null },
  }
  expect(validateDefinition(once, catalog)).toEqual(["仅一次运行需要选择时间"])
  const attr = {
    ...ok,
    aggregation: "DRAMA" as const,
    conditions: [
      { metric: "name", operator: "CONTAINS" as const, values: ["x"] },
    ],
  }
  expect(
    validateDefinition(attr, {
      metrics: [
        ...catalog.metrics,
        {
          key: "name",
          name_zh: "名称",
          kind: "attribute",
          levels: ["adgroup"],
          aggregations: ["EACH"],
          ranged: false,
        },
      ],
    }),
  ).toEqual(["合计口径不能使用属性条件（名称）"])
})

test("汇总发送至少设置一个时刻", () => {
  const def = {
    ...emptyDefinition(),
    conditions: [
      {
        metric: "spend",
        range: "TODAY" as const,
        operator: "GT" as const,
        values: ["10"],
      },
    ],
    notification: {
      enabled: true,
      timing: "SUMMARY" as const,
      summary_times: [],
      notify_without_hits: false,
    },
  }
  expect(validateDefinition(def, catalog)).toEqual(["汇总发送至少设置一个时刻"])
})

test("调整与时段校验与后端一致", () => {
  const base = {
    ...emptyDefinition(),
    conditions: [
      {
        metric: "spend",
        range: "TODAY" as const,
        operator: "GT" as const,
        values: ["10"],
      },
    ],
  }
  const adjust = (adjustment: Record<string, unknown>) => ({
    ...base,
    action: {
      type: "BUDGET" as const,
      level: "adgroup" as const,
      adjustment: adjustment as never,
      frequency: { type: "EVERY_24H" as const },
    },
  })
  expect(
    validateDefinition(
      adjust({
        mode: "INCREASE",
        value_type: "PERCENT",
        value: "10",
        limit: "0",
      }),
      catalog,
    ),
  ).toEqual(["上限需大于 0"])
  expect(
    validateDefinition(
      adjust({ mode: "SET", value_type: "PERCENT", value: "10" }),
      catalog,
    ),
  ).toEqual(["「调整至」只能填写金额"])
  expect(
    validateDefinition(
      adjust({ mode: "SET", value_type: "AMOUNT", value: "10", limit: "5" }),
      catalog,
    ),
  ).toEqual(["「调整至」不设上下限"])
  expect(
    validateDefinition(
      {
        ...base,
        schedule: { ...base.schedule, window: "CUSTOM", periods: [] },
      },
      catalog,
    ),
  ).toEqual(["自定义时段至少添加一个时间段"])
})

test("时区换算覆盖夏令时", () => {
  expect(fromZonedInput("2026-03-08T09:00", "America/New_York")).toBe(
    "2026-03-08T13:00:00.000Z",
  )
  expect(fromZonedInput("2026-03-07T09:00", "America/New_York")).toBe(
    "2026-03-07T14:00:00.000Z",
  )
  expect(toZonedInput("2026-10-14T01:30:00Z", "Asia/Shanghai")).toBe(
    "2026-10-14T09:30",
  )
})

// 关联问题与数据陈旧必须分别说明，不能把部分素材跳过说成全账户无命中。
test("素材数据问题说明具体原因", async () => {
  const { inputIssueText } = await import(
    "../src/features/automation-rules/dataIssues"
  )
  // 零金额排除是可审计说明，不表示止损缺数。
  expect(
    inputIssueText({
      advertiser_id: "acc-1",
      reason: "ZERO_VALUE_UNLINKED_POSTS_IGNORED",
      count: "3",
    }),
  ).toBe(
    "账户 acc-1：已忽略 3 个窗口内无正消耗且无正收入的未关联帖子，不影响规则判断",
  )
  expect(
    inputIssueText({
      advertiser_id: "acc-1",
      reason: "MATERIAL_SHARED_REPORT",
      count: "2",
    }),
  ).toContain("2 个广告内素材")
  expect(
    inputIssueText({
      advertiser_id: "acc-1",
      reason: "MATERIAL_SHARED_REPORT",
      count: "2",
    }),
  ).toContain("共用报表无法拆分")
  expect(
    inputIssueText({
      advertiser_id: "acc-1",
      reason: "MATERIAL_LINK_MISSING",
      count: "1",
    }),
  ).toContain("尚未关联")
})

test("状态条件保存枚举并显示中文摘要", () => {
  const statusCatalog = {
    metrics: [
      ...catalog.metrics,
      {
        key: "ad_status",
        name_zh: "广告状态",
        unit: "status",
        kind: "attribute",
        levels: ["ad", "material"],
        aggregations: ["EACH"],
        ranged: false,
        options: [
          { value: "ENABLE", label: "开启" },
          { value: "DISABLE", label: "暂停" },
        ],
      },
    ],
  }
  const def = {
    ...emptyDefinition(),
    apply_to: { level: "material" as const, status: "PAUSED" as const },
    action: { type: "ENABLE" as const, level: "material" as const },
    schedule: { frequency: "1H" as const, window: "CONTINUOUS" as const },
    conditions: [
      { metric: "ad_status", operator: "EQ" as const, values: ["ENABLE"] },
    ],
  }
  expect(validateDefinition(def, statusCatalog)).toEqual([])
  expect(summarizeConditions(def, statusCatalog)).toBe(
    "每条广告内的素材 · 广告状态 是 开启",
  )
  expect(
    validateDefinition(
      { ...def, conditions: [{ ...def.conditions[0], values: ["0"] }] },
      statusCatalog,
    ),
  ).toEqual(["条件 1 请选择状态"])
})

test("15 分钟仅允许暂停，切换恢复或调整后校验阻止保存", () => {
  const def = emptyDefinition()
  def.conditions[0].values = ["10"]
  def.schedule.frequency = "15M"
  expect(validateDefinition(def, catalog)).toEqual([])
  for (const action of ["ENABLE", "BUDGET", "ROAS"] as const) {
    def.action.type = action
    expect(validateDefinition(def, catalog)).toContain(
      "当前操作最短每 30 分钟运行一次",
    )
  }
  def.action = { type: "ENABLE", level: "material" }
  def.apply_to.level = "material"
  expect(validateDefinition(def, catalog)).toContain(
    "当前操作最短每 1 小时运行一次",
  )
})
