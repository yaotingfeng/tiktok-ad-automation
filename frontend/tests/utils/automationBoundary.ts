import type { Page } from "@playwright/test"

export const TENANT = "11111111-1111-4111-8111-111111111111"
const BC_A = "90071992547409931111"
const token = "automation-rules-test-token"

export type AutomationBoundaryOptions = {
  role?: "tenant_admin" | "operator" | "viewer"
  /** 记录新建规则请求体 */
  created?: unknown[]
  /** 记录通知设置保存请求体 */
  saved?: unknown[]
  /** 记录测试消息请求体 */
  tested?: unknown[]
  /** 记录确认执行请求体 */
  confirmed?: unknown[]
  /** 记录忽略批次的请求路径 */
  dismissed?: string[]
  /** 记录批次明细请求次数 */
  detailRequests?: string[]
  /** 记录批量操作请求体 */
  batched?: unknown[]
}

const CATALOG = {
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
    {
      key: "name",
      group: "attribute",
      name_zh: "名称",
      unit: "text",
      kind: "attribute",
      levels: ["campaign", "adgroup", "ad", "material"],
      aggregations: ["EACH"],
      operators: ["CONTAINS", "NOT_CONTAINS", "STARTS_WITH", "ENDS_WITH", "EQ"],
      ranged: false,
    },
    {
      key: "budget",
      group: "attribute",
      name_zh: "当前预算",
      unit: "money",
      kind: "attribute",
      levels: ["campaign", "adgroup"],
      aggregations: ["EACH"],
      operators: ["GT", "LT", "BETWEEN", "EQ"],
      ranged: false,
    },
  ],
  action_levels: {
    PAUSE: ["campaign", "adgroup", "ad", "material"],
    ENABLE: ["campaign", "adgroup", "ad", "material"],
    BUDGET: ["campaign", "adgroup"],
    ROAS: ["campaign"],
  },
}

const DEFINITION = {
  accounts: { mode: "ALL", bc_ids: [], advertiser_ids: [] },
  apply_to: {
    level: "adgroup",
    status: "ACTIVE",
    object_ids: [],
    drama_names: [],
    name_contains: null,
    name_excludes: null,
  },
  aggregation: "DRAMA",
  conditions: [
    { metric: "spend", range: "TODAY", operator: "GT", values: ["100"] },
  ],
  action: {
    type: "PAUSE",
    level: "adgroup",
    adjustment: null,
    frequency: null,
  },
  mode: "NOTIFY",
  max_targets: 200,
  schedule: {
    frequency: "30M",
    daily_at: null,
    window: "CONTINUOUS",
    periods: [],
    once_at: null,
  },
  notification: {
    enabled: true,
    timing: "EVERY_RUN",
    notify_without_hits: false,
  },
}

const RUN = {
  id: "run-1",
  rule_id: "rule-1",
  rule_name: "剧级止损",
  slot_at: "2026-10-14T02:00:00Z",
  status: "COMPLETE",
  started_at: "2026-10-14T02:00:05Z",
  finished_at: "2026-10-14T02:00:09Z",
  checked_count: 3,
  hit_count: 1,
  skipped_count: 0,
  skipped_accounts: [],
  error_code: null,
  notify_status: "SENT",
  notify_error: null,
  mode: "NOTIFY",
  execution_status: "NOT_REQUIRED",
  execution_summary: {},
}

const EXECUTED_RUN = {
  ...RUN,
  id: "run-2",
  rule_name: "自动加预算",
  mode: "AUTO",
  execution_status: "PARTIAL",
  execution_summary: {
    submitted: 2,
    skipped_frequency: 0,
    skipped_cooldown: 0,
    skipped_daily_limit: 1,
    conflicts: 1,
    no_change: 0,
    failed: 0,
    failed_bcs: [],
    tasks: [{ bc_id: BC_A, task_id: "task-1" }],
    conflict_details: [
      {
        target_key: "adgroup:acc-1:g2",
        rule_name: "夜间暂停",
        action: "PAUSE",
      },
    ],
  },
}

const CONFIRMATION = {
  id: "conf-1",
  rule_id: "rule-1",
  rule_name: "剧级止损",
  run_id: "run-1",
  status: "PENDING",
  created_at: "2026-10-14T02:00:09Z",
  hit_count: 2,
  target_count: 3,
}

const RULE = {
  id: "rule-1",
  name: "剧级止损",
  enabled: true,
  version: 1,
  definition: DEFINITION,
  created_by: "44444444-4444-4444-8444-444444444444",
  updated_by: "44444444-4444-4444-8444-444444444444",
  created_at: "2026-10-14T01:00:00Z",
  updated_at: "2026-10-14T01:00:00Z",
  next_run_at: "2026-10-14T02:30:00Z",
  last_run_at: "2026-10-14T02:00:09Z",
  last_run: RUN,
}

const AUTO_RULE = {
  ...RULE,
  id: "rule-3",
  name: "跑量加预算",
  definition: {
    ...DEFINITION,
    aggregation: "EACH",
    apply_to: { ...DEFINITION.apply_to, level: "campaign" },
    action: {
      type: "BUDGET",
      level: "campaign",
      adjustment: {
        mode: "INCREASE",
        value_type: "PERCENT",
        value: "20",
        limit: "1000",
      },
      frequency: { type: "EVERY_24H", window: null, unit: null, count: null },
    },
    mode: "AUTO",
  },
  last_run: null,
}

const HIT = {
  subject_key: "drama:嘉书-总裁归来:USD",
  subject_label: "总裁归来",
  drama_name: "嘉书-总裁归来",
  advertiser_id: null,
  bc_id: null,
  currency: "USD",
  metrics: { "spend:TODAY": "120" },
  target_count: 1,
  targets: [
    {
      key: "adgroup:acc-1:g1",
      kind: "adgroup",
      advertiser_id: "acc-1",
      bc_id: BC_A,
      remote_id: "g1",
      name: "组一",
      material_uses: [],
    },
  ],
}

export async function automationBoundary(
  page: Page,
  options: AutomationBoundaryOptions = {},
) {
  await page.addInitScript(
    (value) => localStorage.setItem("access_token", value),
    token,
  )
  await page.route("**/api/**", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const method = request.method()
    const headers = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "*",
      "Access-Control-Allow-Methods": "*",
    }
    const json = (body: unknown, status = 200) =>
      route.fulfill({ headers, status, json: body })
    const base = `/api/tenants/${TENANT}`
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers })
    if (path === "/api/users/me")
      return json({
        id: "44444444-4444-4444-8444-444444444444",
        username: "rules-test",
        full_name: "规则测试",
        is_active: true,
        is_superuser: false,
      })
    if (path === "/api/me/tenants")
      return json({
        items: [
          {
            id: TENANT,
            name: "规则租户",
            active: true,
            role: options.role ?? "tenant_admin",
            default_bc_id: BC_A,
          },
        ],
        next_cursor: null,
        total: 1,
      })
    if (path === `${base}/bcs`)
      return json({
        items: [{ bc_id: BC_A, name: "甲 BC" }],
        next_cursor: null,
        total: 1,
      })
    if (path === `${base}/automation-rules/catalog`) return json(CATALOG)
    if (path === `${base}/automation-rules/overview`)
      return json({
        actions_7d: { PAUSE: 12, ENABLE: 3, BUDGET: 5, ROAS: 0 },
        pending_confirmations: 1,
        failed_7d: 2,
        conflicts_7d: 4,
      })
    if (path === `${base}/automation-rules/batch`) {
      const body = request.postDataJSON()
      options.batched?.push(body)
      return json({ updated: body.rule_ids.length, skipped: [] })
    }
    if (path === `${base}/automation-rules/templates`)
      return json({
        items: [
          {
            key: "drama_stop_loss",
            name: "剧级止损",
            description:
              "每部剧今天合计消耗较高且 ROAS 偏低时，暂停该剧的广告组。",
            definition: {
              ...DEFINITION,
              conditions: [
                {
                  metric: "spend",
                  range: "TODAY",
                  operator: "GT",
                  values: ["100"],
                },
                {
                  metric: "roas_total",
                  range: "TODAY",
                  operator: "LT",
                  values: ["0.8"],
                },
              ],
            },
          },
        ],
      })
    if (path === `${base}/automation-rules/dry-run`)
      return json({
        checked_count: 3,
        hit_count: 1,
        skipped_count: 0,
        skipped_accounts: [{ advertiser_id: "acc-9", reason: "STALE_REPORT" }],
        hits: [HIT],
      })
    if (path === `${base}/automation-rules` && method === "POST") {
      const body = request.postDataJSON()
      options.created?.push(body)
      return json({ ...RULE, id: "rule-2", name: body.name }, 201)
    }
    if (path === `${base}/automation-rules`)
      return json({ items: [RULE, AUTO_RULE], total: 2 })
    if (path.startsWith(`${base}/automation-rules/rule-1`)) return json(RULE)
    if (path === `${base}/automation-runs`)
      return json({ items: [RUN, EXECUTED_RUN], total: 2 })
    if (path === `${base}/automation-runs/run-1`)
      return json({ run: RUN, hits: [HIT], confirmation_id: null })
    if (path === `${base}/automation-runs/run-2`)
      return json({
        run: EXECUTED_RUN,
        hits: [{ ...HIT, outcome: "SUBMITTED" }],
        confirmation_id: null,
      })
    if (path === `${base}/automation-confirmations`)
      return json({ items: [CONFIRMATION], total: 1 })
    if (path === `${base}/automation-confirmations/conf-1/confirm`) {
      options.confirmed?.push(request.postDataJSON())
      return json({ ...CONFIRMATION, status: "CONFIRMED" })
    }
    if (path === `${base}/automation-confirmations/conf-1/dismiss`) {
      options.dismissed?.push(path)
      return json({ ...CONFIRMATION, status: "DISMISSED" })
    }
    if (path === `${base}/automation-confirmations/conf-1`) {
      options.detailRequests?.push(path)
      return json({
        confirmation: CONFIRMATION,
        action: DEFINITION.action,
        skipped_accounts: [],
        items: [
          {
            subject_key: "drama:嘉书-总裁归来:USD",
            subject_label: "总裁归来",
            drama_name: "嘉书-总裁归来",
            advertiser_id: null,
            target_count: 2,
            metrics_then: { "spend:TODAY": "120" },
            // 明细每次按最新数据重新判断，现在的指标会变化。
            metrics_now: {
              "spend:TODAY": String(
                134 + (options.detailRequests?.length ?? 1),
              ),
            },
            still_matching: true,
          },
          {
            subject_key: "drama:嘉书-闪婚:USD",
            subject_label: "闪婚",
            drama_name: "嘉书-闪婚",
            advertiser_id: null,
            target_count: 1,
            metrics_then: { "spend:TODAY": "101" },
            metrics_now: {},
            still_matching: false,
          },
        ],
      })
    }
    if (path === `${base}/automation-settings/test-feishu`) {
      options.tested?.push(request.postDataJSON())
      return route.fulfill({ headers, status: 204 })
    }
    if (path === `${base}/automation-settings`) {
      if (method === "PUT") options.saved?.push(request.postDataJSON())
      return json({
        schedule_timezone: "Asia/Shanghai",
        paused: false,
        feishu_configured: true,
        feishu_enabled: true,
        feishu_url_hint: "https://open.feishu.cn/open-apis/bot/v2/hook/…ab12",
        feishu_has_secret: false,
        reverse_cooldown_hours: 24,
        daily_increase_limit_pct: 100,
      })
    }
    return json({ items: [], next_cursor: null, total: 0 })
  })
}
