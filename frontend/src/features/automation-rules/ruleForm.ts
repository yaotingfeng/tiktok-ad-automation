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
  metrics: (Pick<
    CatalogMetric,
    "key" | "name_zh" | "levels" | "aggregations" | "ranged" | "kind"
  > &
    Partial<Pick<CatalogMetric, "unit">>)[]
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
  if (def.mode === "NOTIFY") return NOTIFY_ACTION_LABEL
  return `${ACTION_LABELS[def.action.type]} → ${LEVEL_LABELS[def.action.level]}`
}

/** 「执行操作」下拉里的仅发送通知选项；规则定义中用执行模式 NOTIFY 表示。 */
export const NOTIFY_ACTION = "NOTIFY"
export const NOTIFY_ACTION_LABEL = "仅发送通知"
export type ActionChoice = ActionType | typeof NOTIFY_ACTION

export function actionChoice(def: Definition): ActionChoice {
  return def.mode === "NOTIFY" ? NOTIFY_ACTION : def.action.type
}

/** 切换执行操作：仅发送通知时通知必须开启；改广告的操作默认确认后执行。 */
export function applyActionChoice(draft: Definition, choice: ActionChoice) {
  if (choice === NOTIFY_ACTION) {
    // 仅通知不改广告，动作只用于计算命中涉及的对象，固定为判断层级本身。
    const allowed = actionLevels(
      draft.aggregation,
      draft.apply_to.level,
      "PAUSE",
    )
    draft.mode = "NOTIFY"
    draft.action = { type: "PAUSE", level: allowed[0] ?? draft.apply_to.level }
    draft.notification = { ...draft.notification, enabled: true }
    return draft
  }
  if (draft.mode === "NOTIFY") draft.mode = "CONFIRM"
  const allowed = actionLevels(draft.aggregation, draft.apply_to.level, choice)
  draft.action = {
    type: choice,
    level: allowed.includes(draft.action.level)
      ? draft.action.level
      : (allowed[0] ?? draft.action.level),
    ...(choice === "BUDGET" || choice === "ROAS"
      ? {
          adjustment: {
            mode: "INCREASE",
            value_type: "PERCENT",
            value: "",
            limit: "",
          },
          frequency: { type: "EVERY_24H" },
        }
      : {}),
  }
  return draft
}

export function summarizeScope(def: Definition) {
  const accounts = def.accounts
  if (accounts.mode === "ALL") return "全部账户"
  if (accounts.mode === "BC") return `${accounts.bc_ids?.length ?? 0} 个 BC`
  return `${accounts.advertiser_ids?.length ?? 0} 个账户`
}

const TEXT_OPERATORS: Operator[] = [
  "CONTAINS",
  "NOT_CONTAINS",
  "STARTS_WITH",
  "ENDS_WITH",
]
const FREQUENCY_RANK: Frequency[] = ["30M", "1H", "2H", "4H", "DAILY"]

function blank(value: unknown) {
  return value === null || value === undefined || String(value).trim() === ""
}

function notNumber(value: unknown) {
  return blank(value) || !Number.isFinite(Number(value))
}

/** 保存与试运行前的校验，规则与后端定义一致；返回中文错误，空数组表示通过。 */
export function validateDefinition(
  def: Definition,
  catalog: CatalogLike,
): string[] {
  const errors: string[] = []
  if (def.accounts.mode === "BC" && !def.accounts.bc_ids?.length)
    errors.push("按 BC 选择时至少选择一个 BC")
  if (def.accounts.mode === "ACCOUNTS" && !def.accounts.advertiser_ids?.length)
    errors.push("指定账户时至少填写一个账户 ID")
  if (def.apply_to.status === "SELECTED" && !def.apply_to.object_ids?.length)
    errors.push("指定对象时至少填写一个对象")
  def.conditions.forEach((condition, index) => {
    const label = `条件 ${index + 1}`
    const metric = catalog.metrics.find((item) => item.key === condition.metric)
    if (metric?.kind === "attribute" && def.aggregation !== "EACH")
      errors.push(`合计口径不能使用属性条件（${metric.name_zh}）`)
    const textual =
      metric?.unit === "text" || TEXT_OPERATORS.includes(condition.operator)
    const [first, second] = condition.values
    if (condition.operator === "BETWEEN") {
      if (notNumber(first) || notNumber(second))
        errors.push(`${label} 需要填写上下限`)
      else if (Number(first) >= Number(second))
        errors.push(`${label} 的下限必须小于上限`)
    } else if (textual ? blank(first) : notNumber(first)) {
      errors.push(`${label} 需要填写数值`)
    }
  })
  const adjustment = def.action.adjustment
  if (
    (def.action.type === "BUDGET" || def.action.type === "ROAS") &&
    adjustment
  ) {
    if (notNumber(adjustment.value) || Number(adjustment.value) <= 0)
      errors.push("调整数值需大于 0")
    if (
      adjustment.mode !== "SET" &&
      !notNumber(adjustment.limit) &&
      Number(adjustment.limit) <= 0
    )
      errors.push(
        adjustment.mode === "INCREASE" ? "上限需大于 0" : "下限需大于 0",
      )
    if (adjustment.mode === "SET" && adjustment.value_type !== "AMOUNT")
      errors.push("「调整至」只能填写金额")
    if (adjustment.mode === "SET" && !blank(adjustment.limit))
      errors.push("「调整至」不设上下限")
    if (adjustment.mode === "INCREASE" && notNumber(adjustment.limit))
      errors.push(
        def.action.type === "BUDGET"
          ? "提高预算需要设置上限"
          : "提高出价需要设置上限",
      )
    if (adjustment.mode === "DECREASE" && notNumber(adjustment.limit))
      errors.push(
        def.action.type === "BUDGET"
          ? "降低预算需要设置下限"
          : "降低出价需要设置下限",
      )
    const frequency = def.action.frequency
    if (
      frequency?.type === "CUSTOM" &&
      (blank(frequency.window) ||
        blank(frequency.unit) ||
        blank(frequency.count))
    )
      errors.push("自定义频控需要填写时间窗口和次数")
  }
  const schedule = def.schedule
  if (schedule.frequency === "DAILY" && blank(schedule.daily_at))
    errors.push("每天固定时刻需要选择时间")
  if (schedule.window === "CUSTOM" && !schedule.periods?.length)
    errors.push("自定义时段至少添加一个时间段")
  if (schedule.window === "ONCE" && blank(schedule.once_at))
    errors.push("仅一次运行需要选择时间")
  const notification = def.notification
  if (
    notification?.timing === "SUMMARY" &&
    !(notification.summary_times ?? []).filter(Boolean).length
  )
    errors.push("汇总发送至少设置一个时刻")
  if (
    FREQUENCY_RANK.indexOf(schedule.frequency) <
    FREQUENCY_RANK.indexOf(minFrequency(def.apply_to.level, def.action.level))
  )
    errors.push("涉及素材的规则最短 1 小时运行一次")
  return errors
}

/** UTC ISO 时间转为某时区的 datetime-local 文本（YYYY-MM-DDTHH:mm）。 */
export function toZonedInput(iso: string, timeZone: string) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    })
      .formatToParts(new Date(iso))
      .map((part) => [part.type, part.value]),
  )
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`
}

/** 某时区的 datetime-local 文本转为 UTC ISO；先按 UTC 猜测，再用该时区偏移校正两次（覆盖夏令时）。 */
export function fromZonedInput(value: string, timeZone: string) {
  const target = Date.parse(`${value}:00Z`)
  let guess = target
  for (let i = 0; i < 2; i++) {
    const shown = Date.parse(
      `${toZonedInput(new Date(guess).toISOString(), timeZone)}:00Z`,
    )
    guess += target - shown
  }
  return new Date(guess).toISOString()
}
