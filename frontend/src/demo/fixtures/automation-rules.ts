import { demoUuid, isoDaysAgo } from "../runtime/clock"
import { DEMO_DRAMAS, demoWorld, MAIN_BC_ID, seeded } from "./world"

// 自动化规则私有数据：规则、运行记录、待确认批次

export interface DemoRule {
  id: string
  name: string
  enabled: boolean
  version: number
  definition: Record<string, unknown>
  created_by: string
  updated_by: string
  created_at: string
  updated_at: string
  next_run_at: string | null
  last_run_at: string | null
  last_run: DemoRun | null
}

export interface DemoRun {
  id: string
  rule_id: string
  rule_name: string
  slot_at: string
  status: "COMPLETE" | "RUNNING" | "ERROR"
  started_at: string
  finished_at: string | null
  checked_count: number
  hit_count: number
  skipped_count: number
  skipped_accounts: Array<{ advertiser_id: string; reason: string }>
  error_code: string | null
  notify_status: "SENT" | "FAILED" | "NOT_CONFIGURED" | null
  notify_error: string | null
  mode: "NOTIFY" | "AUTO" | "CONFIRM"
  execution_status:
    | "NOT_REQUIRED"
    | "COMPLETE"
    | "PARTIAL"
    | "FAILED"
    | "PENDING"
  execution_summary: Record<string, unknown>
}

export interface DemoConfirmation {
  id: string
  rule_id: string
  rule_name: string
  run_id: string
  status: "PENDING" | "CONFIRMED" | "DISMISSED"
  created_at: string
  hit_count: number
  target_count: number
}

export interface DemoHit {
  subject_key: string
  subject_label: string
  drama_name: string | null
  advertiser_id: string | null
  bc_id: string | null
  currency: string
  metrics: Record<string, string>
  target_count: number
  targets: Array<{
    key: string
    kind: string
    advertiser_id: string
    bc_id: string
    remote_id: string
    name: string
    material_uses: string[]
  }>
  outcome?: string
}

// 预置规则定义模板
const DEMO_USER_ID = demoUuid(1)

const baseDefinition = {
  accounts: { mode: "ALL", bc_ids: [], advertiser_ids: [] },
  apply_to: {
    level: "adgroup",
    status: "ACTIVE",
    object_ids: [],
  },
  aggregation: "DRAMA",
  action: {
    type: "PAUSE",
    level: "adgroup",
    adjustment: null,
    frequency: null,
  },
  mode: "NOTIFY",
  schedule: {
    frequency: "30M",
    daily_at: null,
    window: "CONTINUOUS",
    periods: [],
    once_at: null,
    timezone_basis: "RULE",
  },
  notification: {
    enabled: true,
    timing: "EVERY_RUN",
    notify_without_hits: false,
  },
}

// 预置 8 条规则，覆盖不同模板和状态
export const demoRules: DemoRule[] = [
  {
    id: demoUuid(501),
    name: "剧级 ROAS 止损",
    enabled: true,
    version: 1,
    definition: {
      ...baseDefinition,
      aggregation: "DRAMA",
      conditions: [
        { metric: "spend", range: "TODAY", operator: "GT", values: ["150"] },
        {
          metric: "roas_total",
          range: "TODAY",
          operator: "LT",
          values: ["0.75"],
        },
      ],
      action: {
        type: "PAUSE",
        level: "adgroup",
        adjustment: null,
        frequency: null,
      },
      mode: "NOTIFY",
    },
    created_by: DEMO_USER_ID,
    updated_by: DEMO_USER_ID,
    created_at: isoDaysAgo(15, 9),
    updated_at: isoDaysAgo(15, 9),
    next_run_at: null,
    last_run_at: isoDaysAgo(0, 14, 30),
    last_run: null,
  },
  {
    id: demoUuid(502),
    name: "单组消耗达标加预算",
    enabled: true,
    version: 2,
    definition: {
      ...baseDefinition,
      aggregation: "EACH",
      apply_to: { level: "adgroup", status: "ACTIVE", object_ids: [] },
      conditions: [
        { metric: "spend", range: "TODAY", operator: "GT", values: ["80"] },
        {
          metric: "roas_total",
          range: "TODAY",
          operator: "GT",
          values: ["1.2"],
        },
      ],
      action: {
        type: "BUDGET",
        level: "adgroup",
        adjustment: {
          mode: "INCREASE",
          value_type: "PERCENT",
          value: "20",
          limit: "500",
        },
        frequency: { type: "EVERY_24H", window: null, unit: null, count: null },
      },
      mode: "AUTO",
      schedule: {
        ...baseDefinition.schedule,
        frequency: "1H",
        timezone_basis: "ACCOUNT",
      },
    },
    created_by: DEMO_USER_ID,
    updated_by: DEMO_USER_ID,
    created_at: isoDaysAgo(12, 10),
    updated_at: isoDaysAgo(5, 16),
    next_run_at: null,
    last_run_at: isoDaysAgo(0, 15, 0),
    last_run: null,
  },
  {
    id: demoUuid(503),
    name: "夜间降预算",
    enabled: true,
    version: 1,
    definition: {
      ...baseDefinition,
      aggregation: "EACH",
      apply_to: { level: "campaign", status: "ACTIVE", object_ids: [] },
      conditions: [
        { metric: "budget", range: null, operator: "GT", values: ["100"] },
      ],
      action: {
        type: "BUDGET",
        level: "campaign",
        adjustment: {
          mode: "DECREASE",
          value_type: "PERCENT",
          value: "50",
          limit: "50",
        },
        frequency: null,
      },
      mode: "AUTO",
      schedule: {
        frequency: "DAILY",
        daily_at: "22:00",
        window: "SCHEDULED",
        periods: [],
        once_at: null,
        timezone_basis: "RULE",
      },
    },
    created_by: DEMO_USER_ID,
    updated_by: DEMO_USER_ID,
    created_at: isoDaysAgo(20, 11),
    updated_at: isoDaysAgo(20, 11),
    next_run_at: isoDaysAgo(-1, 22, 0),
    last_run_at: isoDaysAgo(0, 22, 0),
    last_run: null,
  },
  {
    id: demoUuid(504),
    name: "早晨恢复预算",
    enabled: true,
    version: 1,
    definition: {
      ...baseDefinition,
      aggregation: "EACH",
      apply_to: { level: "campaign", status: "ACTIVE", object_ids: [] },
      conditions: [
        { metric: "budget", range: null, operator: "LT", values: ["100"] },
      ],
      action: {
        type: "BUDGET",
        level: "campaign",
        adjustment: {
          mode: "SET",
          value_type: "ABSOLUTE",
          value: "200",
          limit: null,
        },
        frequency: null,
      },
      mode: "AUTO",
      schedule: {
        frequency: "DAILY",
        daily_at: "08:00",
        window: "SCHEDULED",
        periods: [],
        once_at: null,
        timezone_basis: "RULE",
      },
    },
    created_by: DEMO_USER_ID,
    updated_by: DEMO_USER_ID,
    created_at: isoDaysAgo(20, 11, 15),
    updated_at: isoDaysAgo(20, 11, 15),
    next_run_at: isoDaysAgo(-1, 8, 0),
    last_run_at: isoDaysAgo(0, 8, 0),
    last_run: null,
  },
  {
    id: demoUuid(505),
    name: "新广告启用确认",
    enabled: false,
    version: 1,
    definition: {
      ...baseDefinition,
      aggregation: "EACH",
      apply_to: { level: "ad", status: "PAUSED", object_ids: [] },
      conditions: [
        {
          metric: "ad_status",
          range: null,
          operator: "EQ",
          values: ["DISABLE"],
        },
      ],
      action: {
        type: "ENABLE",
        level: "ad",
        adjustment: null,
        frequency: null,
      },
      mode: "CONFIRM",
    },
    created_by: DEMO_USER_ID,
    updated_by: DEMO_USER_ID,
    created_at: isoDaysAgo(8, 14),
    updated_at: isoDaysAgo(8, 14),
    next_run_at: null,
    last_run_at: null,
    last_run: null,
  },
  {
    id: demoUuid(506),
    name: "周末特定剧加预算",
    enabled: true,
    version: 1,
    definition: {
      ...baseDefinition,
      aggregation: "DRAMA",
      conditions: [
        { metric: "spend", range: "TODAY", operator: "GT", values: ["100"] },
        {
          metric: "roas_total",
          range: "TODAY",
          operator: "GT",
          values: ["1.5"],
        },
      ],
      action: {
        type: "BUDGET",
        level: "adgroup",
        adjustment: {
          mode: "INCREASE",
          value_type: "PERCENT",
          value: "30",
          limit: "800",
        },
        frequency: { type: "EVERY_24H", window: null, unit: null, count: null },
      },
      mode: "CONFIRM",
      schedule: {
        frequency: "DAILY",
        daily_at: "10:00",
        window: "CUSTOM",
        periods: [{ date_type: "WEEK", days: [6, 7] }],
        once_at: null,
        timezone_basis: "RULE",
      },
    },
    created_by: DEMO_USER_ID,
    updated_by: DEMO_USER_ID,
    created_at: isoDaysAgo(10, 13),
    updated_at: isoDaysAgo(10, 13),
    next_run_at: null,
    last_run_at: isoDaysAgo(2, 10, 0),
    last_run: null,
  },
  {
    id: demoUuid(507),
    name: "素材低 ROAS 暂停",
    enabled: true,
    version: 1,
    definition: {
      ...baseDefinition,
      aggregation: "EACH",
      apply_to: { level: "material", status: "ACTIVE", object_ids: [] },
      conditions: [
        { metric: "spend", range: "L3D", operator: "GT", values: ["50"] },
        { metric: "roas_total", range: "L3D", operator: "LT", values: ["0.6"] },
      ],
      action: {
        type: "PAUSE",
        level: "material",
        adjustment: null,
        frequency: null,
      },
      mode: "NOTIFY",
      schedule: { ...baseDefinition.schedule, frequency: "6H" },
    },
    created_by: DEMO_USER_ID,
    updated_by: DEMO_USER_ID,
    created_at: isoDaysAgo(7, 11),
    updated_at: isoDaysAgo(7, 11),
    next_run_at: null,
    last_run_at: isoDaysAgo(0, 12, 0),
    last_run: null,
  },
  {
    id: demoUuid(508),
    name: "测试规则-已停用",
    enabled: false,
    version: 3,
    definition: {
      ...baseDefinition,
      conditions: [
        { metric: "spend", range: "TODAY", operator: "GT", values: ["10"] },
      ],
    },
    created_by: DEMO_USER_ID,
    updated_by: DEMO_USER_ID,
    created_at: isoDaysAgo(3, 16),
    updated_at: isoDaysAgo(1, 10),
    next_run_at: null,
    last_run_at: null,
    last_run: null,
  },
]

// 预置运行记录
export const demoRuns: DemoRun[] = [
  {
    id: demoUuid(601),
    rule_id: demoRules[0].id,
    rule_name: demoRules[0].name,
    slot_at: isoDaysAgo(0, 14, 30),
    status: "COMPLETE",
    started_at: isoDaysAgo(0, 14, 35),
    finished_at: isoDaysAgo(0, 14, 42),
    checked_count: 24,
    hit_count: 2,
    skipped_count: 0,
    skipped_accounts: [],
    error_code: null,
    notify_status: "SENT",
    notify_error: null,
    mode: "NOTIFY",
    execution_status: "NOT_REQUIRED",
    execution_summary: {},
  },
  {
    id: demoUuid(602),
    rule_id: demoRules[1].id,
    rule_name: demoRules[1].name,
    slot_at: isoDaysAgo(0, 15, 0),
    status: "COMPLETE",
    started_at: isoDaysAgo(0, 15, 3),
    finished_at: isoDaysAgo(0, 15, 9),
    checked_count: 18,
    hit_count: 3,
    skipped_count: 1,
    skipped_accounts: [
      { advertiser_id: "7431000000000001003", reason: "STALE_REPORT" },
    ],
    error_code: null,
    notify_status: "SENT",
    notify_error: null,
    mode: "AUTO",
    execution_status: "PARTIAL",
    execution_summary: {
      submitted: 2,
      skipped_frequency: 1,
      conflicts: 0,
      no_change: 0,
      failed: 0,
      failed_bcs: [],
      tasks: [{ bc_id: MAIN_BC_ID, task_id: demoUuid(701) }],
      conflict_details: [],
    },
  },
  {
    id: demoUuid(603),
    rule_id: demoRules[2].id,
    rule_name: demoRules[2].name,
    slot_at: isoDaysAgo(0, 22, 0),
    status: "COMPLETE",
    started_at: isoDaysAgo(0, 22, 2),
    finished_at: isoDaysAgo(0, 22, 8),
    checked_count: 15,
    hit_count: 8,
    skipped_count: 0,
    skipped_accounts: [],
    error_code: null,
    notify_status: "NOT_CONFIGURED",
    notify_error: null,
    mode: "AUTO",
    execution_status: "COMPLETE",
    execution_summary: {
      submitted: 8,
      skipped_frequency: 0,
      conflicts: 0,
      no_change: 0,
      failed: 0,
      failed_bcs: [],
      tasks: [{ bc_id: MAIN_BC_ID, task_id: demoUuid(702) }],
      conflict_details: [],
    },
  },
  {
    id: demoUuid(604),
    rule_id: demoRules[5].id,
    rule_name: demoRules[5].name,
    slot_at: isoDaysAgo(2, 10, 0),
    status: "COMPLETE",
    started_at: isoDaysAgo(2, 10, 4),
    finished_at: isoDaysAgo(2, 10, 10),
    checked_count: 12,
    hit_count: 1,
    skipped_count: 0,
    skipped_accounts: [],
    error_code: null,
    notify_status: "SENT",
    notify_error: null,
    mode: "CONFIRM",
    execution_status: "PENDING",
    execution_summary: {},
  },
  {
    id: demoUuid(605),
    rule_id: demoRules[0].id,
    rule_name: demoRules[0].name,
    slot_at: isoDaysAgo(0, 14, 0),
    status: "COMPLETE",
    started_at: isoDaysAgo(0, 14, 5),
    finished_at: isoDaysAgo(0, 14, 11),
    checked_count: 24,
    hit_count: 0,
    skipped_count: 0,
    skipped_accounts: [],
    error_code: null,
    notify_status: null,
    notify_error: null,
    mode: "NOTIFY",
    execution_status: "NOT_REQUIRED",
    execution_summary: {},
  },
  {
    id: demoUuid(606),
    rule_id: demoRules[6].id,
    rule_name: demoRules[6].name,
    slot_at: isoDaysAgo(0, 12, 0),
    status: "COMPLETE",
    started_at: isoDaysAgo(0, 12, 3),
    finished_at: isoDaysAgo(0, 12, 7),
    checked_count: 20,
    hit_count: 1,
    skipped_count: 0,
    skipped_accounts: [],
    error_code: null,
    notify_status: "SENT",
    notify_error: null,
    mode: "NOTIFY",
    execution_status: "NOT_REQUIRED",
    execution_summary: {},
  },
  {
    id: demoUuid(607),
    rule_id: demoRules[1].id,
    rule_name: demoRules[1].name,
    slot_at: isoDaysAgo(1, 16, 0),
    status: "ERROR",
    started_at: isoDaysAgo(1, 16, 3),
    finished_at: isoDaysAgo(1, 16, 5),
    checked_count: 0,
    hit_count: 0,
    skipped_count: 0,
    skipped_accounts: [],
    error_code: "REPORT_UNAVAILABLE",
    notify_status: null,
    notify_error: null,
    mode: "AUTO",
    execution_status: "NOT_REQUIRED",
    execution_summary: {},
  },
]

// 预置待确认批次（1 条待确认，1 条已确认）
export const demoConfirmations: DemoConfirmation[] = [
  {
    id: demoUuid(801),
    rule_id: demoRules[5].id,
    rule_name: demoRules[5].name,
    run_id: demoRuns[3].id,
    status: "PENDING",
    created_at: isoDaysAgo(2, 10, 10),
    hit_count: 1,
    target_count: 3,
  },
  {
    id: demoUuid(802),
    rule_id: demoRules[1].id,
    rule_name: demoRules[1].name,
    run_id: demoUuid(699),
    status: "CONFIRMED",
    created_at: isoDaysAgo(3, 11, 30),
    hit_count: 2,
    target_count: 4,
  },
]

// 生成命中明细样例（从 demoWorld.launched 取实际数据）
export function makeHits(_ruleId: string, count: number): DemoHit[] {
  const hits: DemoHit[] = []
  const campaigns = demoWorld.launched.slice(0, count + 2)

  for (let i = 0; i < Math.min(count, campaigns.length); i++) {
    const campaign = campaigns[i]
    const drama = DEMO_DRAMAS.find((d) => d.id === campaign.drama_id)
    if (!drama) continue

    const spend = Math.round(120 + seeded(`hit${i}`) * 180)
    const roas = (0.5 + seeded(`roas${i}`) * 0.8).toFixed(2)

    hits.push({
      subject_key: `drama:${drama.provider_kind}-${drama.title_zh}:USD`,
      subject_label: drama.title_zh,
      drama_name: `${drama.provider_kind}-${drama.title_zh}`,
      advertiser_id: null,
      bc_id: null,
      currency: "USD",
      metrics: { "spend:TODAY": String(spend), "roas_total:TODAY": roas },
      target_count: campaign.adgroups.length,
      targets: campaign.adgroups.map((g) => ({
        key: `adgroup:${campaign.advertiser_id}:${g.adgroup_id}`,
        kind: "adgroup",
        advertiser_id: campaign.advertiser_id,
        bc_id: campaign.bc_id,
        remote_id: g.adgroup_id,
        name: g.name,
        material_uses: [],
      })),
    })
  }

  return hits
}

// 通知设置
export const demoAutomationSettings = {
  schedule_timezone: "Asia/Shanghai",
  paused: false,
  feishu_configured: true,
  feishu_enabled: true,
  feishu_url_hint: "https://open.feishu.cn/open-apis/bot/v2/hook/…7f3a",
  feishu_has_secret: false,
}
