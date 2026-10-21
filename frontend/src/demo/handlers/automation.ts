import {
  demoAutomationSettings,
  demoConfirmations,
  demoRules,
  demoRuns,
  makeHits,
} from "../fixtures/automation-rules"
import {
  DEMO_ADVERTISERS,
  demoWorld,
  MAIN_BC_ID,
  seeded,
} from "../fixtures/world"
import { demoUuid, isoNow, nextId } from "../runtime/clock"
import { matchesSearch, paginate } from "../runtime/paging"
import { handle } from "../runtime/registry"

// 自动化规则假后端：所有 automation_rules 标签的 22 个 operationId

// ============ 规则列表与 CRUD ============

handle("automation_rules-list_rules", () => {
  // query 在 openapi 中未声明，返回所有规则
  return {
    items: demoRules as any,
    total: demoRules.length,
  }
})

handle("automation_rules-get_rule", (req) => {
  const rule = demoRules.find((r) => r.id.startsWith(req.path.rule_id))
  if (!rule) throw new Error("Rule not found")
  // 附加最后一次运行摘要
  const lastRun = demoRuns.find((run) => run.rule_id === rule.id)
  const lastRunSummary = lastRun
    ? {
        id: lastRun.id,
        slot_at: lastRun.slot_at,
        status: lastRun.status,
        hit_count: lastRun.hit_count,
        execution_status: lastRun.execution_status,
      }
    : null
  return { ...rule, last_run: lastRunSummary } as any
})

handle("automation_rules-create_rule", (req) => {
  const newRule: any = {
    id: demoUuid(nextId()),
    name: req.body.name,
    enabled: req.body.enabled ?? true,
    version: 1,
    definition: req.body.definition,
    created_by: demoUuid(1),
    updated_by: demoUuid(1),
    created_at: isoNow(),
    updated_at: isoNow(),
    next_run_at: null,
    last_run_at: null,
    last_run: null,
  }
  demoRules.unshift(newRule)
  return newRule
})

handle("automation_rules-update_rule", (req): any => {
  const rule = demoRules.find((r) => r.id.startsWith(req.path.rule_id))
  if (!rule) throw new Error("Rule not found")

  if (req.body.name !== undefined) rule.name = req.body.name
  if (req.body.definition !== undefined) {
    rule.definition = req.body.definition
    rule.version += 1
  }
  if (req.body.enabled !== undefined) rule.enabled = req.body.enabled

  rule.updated_by = demoUuid(1)
  rule.updated_at = isoNow()

  return rule
})

handle("automation_rules-delete_rule", (req) => {
  const index = demoRules.findIndex((r) => r.id.startsWith(req.path.rule_id))
  if (index >= 0) demoRules.splice(index, 1)
  return undefined
})

handle("automation_rules-set_enabled", (req) => {
  const rule = demoRules.find((r) => r.id.startsWith(req.path.rule_id))
  if (!rule) throw new Error("Rule not found")

  rule.enabled = req.body.enabled
  rule.updated_by = demoUuid(1)
  rule.updated_at = isoNow()

  return rule as any
})

handle("automation_rules-batch_rules", (req) => {
  const updated: string[] = []
  const skipped: Array<{ rule_id: string; reason: string }> = []

  for (const ruleId of req.body.rule_ids) {
    const rule = demoRules.find((r) => r.id.startsWith(ruleId))
    if (!rule) {
      skipped.push({ rule_id: ruleId, reason: "NOT_FOUND" })
      continue
    }

    if (req.body.operation === "ENABLE") rule.enabled = true
    else if (req.body.operation === "DISABLE") rule.enabled = false
    else if (req.body.operation === "DELETE") {
      const index = demoRules.indexOf(rule)
      if (index >= 0) demoRules.splice(index, 1)
    }

    updated.push(rule.id)
  }

  return { updated: updated.length, skipped }
})

// ============ 规则目录与模板 ============

handle("automation_rules-get_catalog", () => {
  return {
    metrics: [
      // 状态指标
      ...["campaign", "adgroup", "ad", "material"].map((level, index) => ({
        key: `${level}_status`,
        group: "attribute",
        name_zh: ["系列状态", "广告组状态", "广告状态", "素材状态"][index],
        unit: "status",
        kind: "attribute",
        levels: ["campaign", "adgroup", "ad", "material"].slice(index),
        aggregations: ["EACH"],
        operators: ["EQ"],
        ranged: false,
        options: [
          { value: "ENABLE", label: "开启" },
          { value: "DISABLE", label: "暂停" },
        ],
      })),
      // 事实指标
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
        operators: [
          "CONTAINS",
          "NOT_CONTAINS",
          "STARTS_WITH",
          "ENDS_WITH",
          "EQ",
        ],
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
      {
        key: "conversion",
        group: "delivery",
        name_zh: "转化数",
        unit: "count",
        kind: "fact",
        levels: ["campaign", "adgroup", "ad", "material"],
        aggregations: ["EACH", "DRAMA", "ALL"],
        operators: ["GT", "LT", "BETWEEN", "EQ"],
        ranged: true,
      },
      {
        key: "ctr",
        group: "delivery",
        name_zh: "点击率",
        unit: "percentage",
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
})

handle("automation_rules-list_templates", (): any => ({
  items: [
    {
      key: "drama_stop_loss",
      name: "剧级止损",
      description: "每部剧今天合计消耗较高且 ROAS 偏低时，暂停该剧的广告组。",
      definition: {
        accounts: { mode: "ALL", bc_ids: [], advertiser_ids: [] },
        apply_to: { level: "adgroup", status: "ACTIVE", object_ids: [] },
        aggregation: "DRAMA",
        conditions: [
          {
            metric: "spend",
            range: "TODAY",
            operator: "GT",
            values: ["150"],
          },
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
      },
    },
    {
      key: "adgroup_budget_increase",
      name: "广告组跑量加预算",
      description: "单个广告组消耗达标且 ROAS 良好时，自动提升预算。",
      definition: {
        accounts: { mode: "ALL", bc_ids: [], advertiser_ids: [] },
        apply_to: { level: "adgroup", status: "ACTIVE", object_ids: [] },
        aggregation: "EACH",
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
          frequency: {
            type: "EVERY_24H",
            window: null,
            unit: null,
            count: null,
          },
        },
        mode: "CONFIRM",
        schedule: {
          frequency: "1H",
          daily_at: null,
          window: "CONTINUOUS",
          periods: [],
          once_at: null,
          timezone_basis: "ACCOUNT",
        },
        notification: {
          enabled: true,
          timing: "EVERY_RUN",
          notify_without_hits: false,
        },
      },
    },
    {
      key: "night_budget_decrease",
      name: "夜间降预算",
      description: "每晚固定时间降低系列预算，减少夜间消耗。",
      definition: {
        accounts: { mode: "ALL", bc_ids: [], advertiser_ids: [] },
        apply_to: { level: "campaign", status: "ACTIVE", object_ids: [] },
        aggregation: "EACH",
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
          window: "CONTINUOUS" as any,
          periods: [],
          once_at: null,
          timezone_basis: "RULE",
        },
        notification: {
          enabled: false,
          timing: "EVERY_RUN",
          notify_without_hits: false,
        },
      },
    },
    {
      key: "morning_budget_restore",
      name: "早晨恢复预算",
      description: "每早固定时间恢复系列预算到标准值。",
      definition: {
        accounts: { mode: "ALL", bc_ids: [], advertiser_ids: [] },
        apply_to: { level: "campaign", status: "ACTIVE", object_ids: [] },
        aggregation: "EACH",
        conditions: [
          { metric: "budget", range: null, operator: "LT", values: ["100"] },
        ],
        action: {
          type: "BUDGET",
          level: "campaign",
          adjustment: {
            mode: "SET",
            value_type: "PERCENT",
            value: "200",
            limit: null,
          },
          frequency: null,
        },
        mode: "AUTO",
        schedule: {
          frequency: "DAILY",
          daily_at: "08:00",
          window: "CONTINUOUS" as any,
          periods: [],
          once_at: null,
          timezone_basis: "RULE",
        },
        notification: {
          enabled: false,
          timing: "EVERY_RUN",
          notify_without_hits: false,
        },
      },
    },
    {
      key: "material_stop_loss",
      name: "素材低 ROAS 暂停",
      description: "近3天素材消耗达标但 ROAS 低于阈值时，暂停该素材。",
      definition: {
        accounts: { mode: "ALL", bc_ids: [], advertiser_ids: [] },
        apply_to: { level: "material", status: "ACTIVE", object_ids: [] },
        aggregation: "EACH",
        conditions: [
          {
            metric: "spend",
            range: "PAST_3_DAYS",
            operator: "GT",
            values: ["50"],
          },
          {
            metric: "roas_total",
            range: "PAST_3_DAYS",
            operator: "LT",
            values: ["0.6"],
          },
        ],
        action: {
          type: "PAUSE",
          level: "material",
          adjustment: null,
          frequency: null,
        },
        mode: "NOTIFY",
        schedule: {
          frequency: "4H" as any,
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
      },
    },
  ],
}))

handle("automation_rules-get_overview", () => {
  // 统计最近 7 天的执行操作
  const recent = demoRuns.filter((r) => {
    const ago = Date.now() - new Date(r.started_at).getTime()
    return ago < 7 * 24 * 60 * 60 * 1000
  })

  const actions = { PAUSE: 0, ENABLE: 0, BUDGET: 0, ROAS: 0 }
  let failed = 0
  let conflicts = 0

  for (const run of recent) {
    if (run.status === "ERROR") failed++
    if (
      run.execution_status === "COMPLETE" ||
      run.execution_status === "PARTIAL"
    ) {
      const rule = demoRules.find((r) => r.id === run.rule_id)
      if (rule) {
        const actionType = (rule.definition as any).action?.type
        if (actionType && actionType in actions) {
          actions[actionType as keyof typeof actions] += run.hit_count
        }
      }
      const conflictDetails = (run.execution_summary as any)?.conflict_details
      if (Array.isArray(conflictDetails)) {
        conflicts += conflictDetails.length
      }
    }
  }

  const pending = demoConfirmations.filter((c) => c.status === "PENDING").length

  return {
    actions_7d: actions,
    pending_confirmations: pending,
    failed_7d: failed,
    conflicts_7d: conflicts,
  }
})

// ============ 试运行 ============

handle("automation_rules-dry_run", (req) => {
  const hitCount = Math.floor(1 + seeded(JSON.stringify(req.body)) * 3)
  const checked = Math.max(hitCount + 5, 10)
  const skippedCount = Math.floor(seeded(`${checked}skip`) * 2)

  return {
    checked_count: checked,
    hit_count: hitCount,
    skipped_count: skippedCount,
    skipped_accounts:
      skippedCount > 0
        ? [{ advertiser_id: "7431000000000001003", reason: "STALE_REPORT" }]
        : [],
    hits: makeHits("dry-run", hitCount),
  }
})

// ============ 选择器 ============

handle("automation_rules-pick_accounts", (req) => {
  const q = req.search.get("q") ?? ""
  const bcId = req.search.get("bc_id") ?? MAIN_BC_ID

  const accounts = DEMO_ADVERTISERS.filter(
    (a) =>
      a.bc_id === bcId &&
      a.purpose === "delivery" &&
      matchesSearch(q, a.name, a.advertiser_id),
  )

  return accounts.map((a) => ({
    advertiser_id: a.advertiser_id,
    name: a.name,
    bc_id: a.bc_id,
    currency: a.currency,
    timezone: a.timezone,
  }))
})

handle("automation_rules-pick_objects", (req) => {
  const q = req.search.get("q") ?? ""
  const level = req.search.get("level") ?? "adgroup"
  const offset = Number(req.search.get("offset") ?? 0)
  const limit = Number(req.search.get("limit") ?? 50)

  // 从 demoWorld.launched 生成候选对象
  const objects: Array<{
    value: string
    label: string
    advertiser_id: string
    kind: string
    status: string
  }> = []

  if (level === "campaign") {
    demoWorld.launched.forEach((c) => {
      objects.push({
        value: `${c.advertiser_id}:${c.campaign_id}`,
        label: c.name,
        advertiser_id: c.advertiser_id,
        kind: "campaign",
        status: c.status,
      })
    })
  } else if (level === "adgroup") {
    demoWorld.launched.forEach((c) => {
      c.adgroups.forEach((g) => {
        objects.push({
          value: `${c.advertiser_id}:${g.adgroup_id}`,
          label: g.name,
          advertiser_id: c.advertiser_id,
          kind: "adgroup",
          status: g.status,
        })
      })
    })
  } else if (level === "ad") {
    demoWorld.launched.forEach((c) => {
      c.adgroups.forEach((g) => {
        g.ads.forEach((ad) => {
          objects.push({
            value: `${c.advertiser_id}:${ad.ad_id}`,
            label: ad.name,
            advertiser_id: c.advertiser_id,
            kind: "ad",
            status: ad.status,
          })
        })
      })
    })
  }

  const filtered = objects.filter((obj) => matchesSearch(q, obj.label))
  return filtered.slice(offset, offset + limit)
})

// ============ 运行记录 ============

handle("automation_rules-list_runs", (req) => {
  let filtered = [...demoRuns]

  if (req.query?.rule_id) {
    filtered = filtered.filter((r) => r.rule_id.startsWith(req.query.rule_id!))
  }
  // result 参数用于过滤，不是 status
  if (req.query?.result) {
    // result 映射到内部状态
    const result = req.query.result
    if (result === "FAILED") {
      filtered = filtered.filter((r) => r.status === "ERROR")
    } else if (result === "HIT") {
      filtered = filtered.filter((r) => r.hit_count > 0)
    } else if (result === "NO_HIT") {
      filtered = filtered.filter(
        (r) => r.hit_count === 0 && r.status === "COMPLETE",
      )
    }
  }

  // 按时间倒序
  filtered.sort(
    (a, b) =>
      new Date(b.started_at).getTime() - new Date(a.started_at).getTime(),
  )

  return paginate(filtered, req.query ?? {}) as any
})

handle("automation_rules-get_run", (req) => {
  const run = demoRuns.find((r) => r.id.startsWith(req.path.run_id))
  if (!run) throw new Error("Run not found")

  const hits = makeHits(run.rule_id, run.hit_count)

  // 如果是自动执行且有提交，给命中加上 outcome
  if (run.mode === "AUTO" && run.execution_status !== "NOT_REQUIRED") {
    const submitted = (run.execution_summary?.submitted as number) ?? 0
    hits.forEach((hit, i) => {
      if (i < submitted) {
        hit.outcome = "SUBMITTED"
      } else if (i < run.hit_count) {
        hit.outcome = "SKIPPED_FREQUENCY"
      }
    })
  }

  const confirmation = demoConfirmations.find((c) => c.run_id === run.id)

  return {
    run: run as any,
    hits: hits as any,
    confirmation_id: confirmation?.id ?? null,
  }
})

// ============ 待确认批次 ============

handle("automation_rules-list_confirmations", (req) => {
  let filtered = [...demoConfirmations]

  // status 通过 search 读取
  const status = req.search.get("status")
  if (status) {
    filtered = filtered.filter((c) => c.status === status)
  }

  // 按创建时间倒序
  filtered.sort(
    (a, b) =>
      new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  )

  return {
    items: filtered as any,
    total: filtered.length,
  }
})

handle("automation_rules-get_confirmation", (req): any => {
  const confirmation = demoConfirmations.find((c) =>
    c.id.startsWith(req.path.confirmation_id),
  )
  if (!confirmation) throw new Error("Confirmation not found")

  const rule = demoRules.find((r) => r.id === confirmation.rule_id)
  const action = rule ? (rule.definition as any).action : null

  // 生成明细：包含 metrics_then 和 metrics_now
  const hits = makeHits(confirmation.rule_id, confirmation.hit_count)
  const items = hits.map((hit, i) => {
    const spend_then = hit.metrics["spend:TODAY"]
    const spend_now = String(
      Number(spend_then) + Math.round(seeded(`now${i}`) * 30),
    )
    // 多个对象时最后一个演示“条件已不满足”（不会被执行），其余仍满足、可勾选确认。
    const still_matching = hits.length === 1 || i < hits.length - 1

    return {
      subject_key: hit.subject_key,
      subject_label: hit.subject_label,
      drama_name: hit.drama_name,
      advertiser_id: hit.advertiser_id,
      target_count: hit.target_count,
      metrics_then: hit.metrics,
      metrics_now: still_matching ? { "spend:TODAY": spend_now } : {},
      still_matching,
    }
  })

  return {
    confirmation,
    action,
    skipped_accounts: [],
    items,
  }
})

handle("automation_rules-confirm_batch", (req) => {
  const confirmation = demoConfirmations.find((c) =>
    c.id.startsWith(req.path.confirmation_id),
  )
  if (!confirmation) throw new Error("Confirmation not found")

  confirmation.status = "CONFIRMED"

  // 模拟执行：修改对应广告组的预算或状态
  const rule = demoRules.find((r) => r.id === confirmation.rule_id)
  if (rule) {
    const action = (rule.definition as any).action
    if (action?.type === "PAUSE") {
      // 暂停前几个广告组
      const toUpdate = demoWorld.launched.slice(0, 2)
      toUpdate.forEach((c) => {
        c.adgroups.forEach((g) => {
          g.status = "DISABLE"
        })
      })
    } else if (action?.type === "BUDGET") {
      // 提升前几个广告组预算
      const toUpdate = demoWorld.launched.slice(0, 2)
      toUpdate.forEach((c) => {
        c.adgroups.forEach((g) => {
          const increase = Number(action.adjustment?.value ?? 20)
          const limit = Number(action.adjustment?.limit ?? 999)
          g.budget = Math.min(g.budget * (1 + increase / 100), limit)
        })
      })
    }
  }

  return confirmation
})

handle("automation_rules-dismiss_batch", (req) => {
  const confirmation = demoConfirmations.find((c) =>
    c.id.startsWith(req.path.confirmation_id),
  )
  if (!confirmation) throw new Error("Confirmation not found")

  confirmation.status = "DISMISSED"
  return confirmation
})

// ============ 通知设置 ============

handle("automation_rules-get_settings", () => {
  return demoAutomationSettings
})

handle("automation_rules-put_settings", (req) => {
  if (req.body.schedule_timezone !== undefined) {
    demoAutomationSettings.schedule_timezone = req.body.schedule_timezone
  }
  if (req.body.paused !== undefined) {
    demoAutomationSettings.paused = req.body.paused
  }
  if (req.body.webhook_url !== undefined) {
    demoAutomationSettings.feishu_configured = !!req.body.webhook_url
    demoAutomationSettings.feishu_url_hint = req.body.webhook_url
      ? `${req.body.webhook_url.slice(0, 50)}…${req.body.webhook_url.slice(-4)}`
      : ""
  }
  if (req.body.feishu_enabled !== undefined) {
    demoAutomationSettings.feishu_enabled = req.body.feishu_enabled
  }
  if (req.body.secret !== undefined) {
    demoAutomationSettings.feishu_has_secret = !!req.body.secret
  }

  return demoAutomationSettings
})

handle("automation_rules-test_feishu", () => {
  // 测试发送成功，返回 204 (undefined 自动变为 204)
  return undefined
})
