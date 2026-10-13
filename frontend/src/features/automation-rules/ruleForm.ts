import type {
  CatalogMetric,
  CatalogPublic,
  Condition,
  RuleDefinition_Input,
} from "@/client"

export type Definition = RuleDefinition_Input
export type Level = Definition["apply_to"]["level"]
export type Aggregation = Definition["aggregation"]
export type ActionType = Definition["action"]["type"]
export type RangeType = NonNullable<Condition["range"]>
export type Operator = Condition["operator"]
export type Frequency = Definition["schedule"]["frequency"]
type CatalogLike = Pick<CatalogPublic, "metrics"> & {
  metrics: Pick<
    CatalogMetric,
    "key" | "name_zh" | "levels" | "aggregations" | "ranged" | "kind"
  >[]
}

export const LEVELS: Level[] = ["campaign", "adgroup", "ad", "material"]
export const LEVEL_LABELS: Record<Level, string> = {
  campaign: "推广系列",
  adgroup: "广告组",
  ad: "广告",
  material: "素材",
}
export const AGGREGATION_LABELS: Record<Aggregation, string> = {
  EACH: "每个对象",
  DRAMA: "每部剧合计",
  ALL: "所有对象合计",
}
export const ACTION_LABELS: Record<ActionType, string> = {
  PAUSE: "暂停",
  ENABLE: "启用",
  BUDGET: "调整预算",
  ROAS: "调整 ROAS 出价",
}
export const MODE_LABELS: Record<Definition["mode"], string> = {
  AUTO: "自动执行",
  CONFIRM: "确认后执行",
  NOTIFY: "仅通知",
}
export const RANGE_LABELS: Record<RangeType, string> = {
  TODAY: "今天",
  YESTERDAY: "昨天",
  PAST_3_DAYS: "近 3 天",
  PAST_5_DAYS: "近 5 天",
  PAST_7_DAYS: "近 7 天",
  LIFETIME: "投放以来",
}
export const OPERATOR_LABELS: Record<Operator, string> = {
  GT: "大于",
  LT: "小于",
  BETWEEN: "介于",
  EQ: "等于",
  CONTAINS: "包含",
  NOT_CONTAINS: "不包含",
  STARTS_WITH: "开头是",
  ENDS_WITH: "结尾是",
}
export const FREQUENCY_LABELS: Record<Frequency, string> = {
  "30M": "每 30 分钟",
  "1H": "每 1 小时",
  "2H": "每 2 小时",
  "4H": "每 4 小时",
  DAILY: "每天固定时刻",
}
const SYMBOLS: Partial<Record<Operator, string>> = {
  GT: ">",
  LT: "<",
  EQ: "=",
}
// 与后端 catalog.ACTION_LEVELS 一致：ROAS 只在系列层级调整。
const ACTION_LEVELS: Record<ActionType, Level[]> = {
  PAUSE: LEVELS,
  ENABLE: LEVELS,
  BUDGET: ["campaign", "adgroup"],
  ROAS: ["campaign"],
}

export function emptyDefinition(): Definition {
  return {
    accounts: { mode: "ALL", bc_ids: [], advertiser_ids: [] },
    apply_to: { level: "adgroup", status: "ACTIVE", object_ids: [] },
    aggregation: "EACH",
    conditions: [
      { metric: "spend", range: "TODAY", operator: "GT", values: [""] },
    ],
    action: { type: "PAUSE", level: "adgroup" },
    mode: "NOTIFY",
    max_targets: 200,
    schedule: { frequency: "30M", window: "CONTINUOUS", periods: [] },
    notification: {
      enabled: true,
      timing: "EVERY_RUN",
      notify_without_hits: false,
    },
  }
}

/** 当前层级与统计口径下可选的指标；属性类指标只在「每个对象」口径下出现。 */
export function metricOptions<T extends CatalogLike["metrics"][number]>(
  catalog: { metrics: T[] },
  level: Level,
  aggregation: Aggregation,
): T[] {
  return catalog.metrics.filter(
    (metric) =>
      metric.levels.includes(level) &&
      metric.aggregations.includes(aggregation),
  )
}

/** 执行层级必须在判断对象范围内：合计口径不限，单对象口径只能是自身或下级。 */
export function actionLevels(
  aggregation: Aggregation,
  applyLevel: Level,
  actionType: ActionType,
): Level[] {
  const supported = ACTION_LEVELS[actionType]
  if (aggregation !== "EACH") return supported
  const depth = LEVELS.indexOf(applyLevel)
  return supported.filter((level) => LEVELS.indexOf(level) >= depth)
}

/** 素材报表 60 分钟同步一次，涉及素材的规则最短 1 小时运行一次。 */
export function minFrequency(applyLevel: Level, actionLevel: Level): Frequency {
  return applyLevel === "material" || actionLevel === "material" ? "1H" : "30M"
}

function conditionText(condition: Condition, catalog: CatalogLike) {
  const metric = catalog.metrics.find((item) => item.key === condition.metric)
  const name = metric?.name_zh ?? condition.metric
  const prefix = condition.range ? RANGE_LABELS[condition.range] : ""
  const [first, second] = condition.values
  const symbol = SYMBOLS[condition.operator]
  if (condition.operator === "BETWEEN")
    return `${prefix}${name} 介于 ${first} ~ ${second}`
  if (symbol && metric?.kind !== "attribute")
    return `${prefix}${name} ${symbol} ${first}`
  return `${prefix}${name} ${symbol ?? OPERATOR_LABELS[condition.operator]} ${first}`
}

export function summarizeConditions(def: Definition, catalog: CatalogLike) {
  const parts = def.conditions.map((condition) =>
    conditionText(condition, catalog),
  )
  return `${AGGREGATION_LABELS[def.aggregation]} · ${parts.join(" 且 ")}`
}

export function summarizeAction(def: Definition) {
  return `${ACTION_LABELS[def.action.type]} → ${LEVEL_LABELS[def.action.level]}`
}

export function summarizeScope(def: Definition) {
  const accounts = def.accounts
  if (accounts.mode === "ALL") return "全部账户"
  if (accounts.mode === "BC") return `${accounts.bc_ids?.length ?? 0} 个 BC`
  return `${accounts.advertiser_ids?.length ?? 0} 个账户`
}
