import { imageDeliveries, type DemoImageUnit } from "../fixtures/image-delivery"
import type {
  ObjectCounts,
  RecoveryReceipt,
  StepPublic,
  SubmissionAdPublic,
  SubmissionEventPublic,
  SubmissionGroupPublic,
  SubmissionListItem,
  SubmissionMaterialPublic,
  SubmissionUnitPublic,
  SubmissionView,
} from "@/client"
import { demoDrafts, demoPreviews } from "../fixtures/builds-data"
import {
  DEMO_ADVERTISERS,
  DEMO_DRAMAS,
  DEMO_PROVIDERS,
  DEMO_STRATEGIES,
  DEMO_TEAM,
  DEMO_USER,
  type DemoAdvertiser,
  type DemoDrama,
  type DemoLaunchedCampaign,
  type DemoMaterial,
  deliveryAdvertisers,
  demoWorld,
  HISTORY_SUBMISSION_IDS,
  launchCampaigns,
  MAIN_BC_ID,
  materialsOfDrama,
} from "../fixtures/world"
import { demoUuid, isoAt, isoDaysAgo, now } from "../runtime/clock"
import { paginate } from "../runtime/paging"
import { DemoReply, demoError, handle } from "../runtime/registry"

// 体验版假后端：搭建任务（提交、执行进度、明细、失败重试与核查）。
// 每个“剧 × 账户”组合是一个执行单元，按时间依次完成；单元完成时在 demoWorld 里上线对应
// Campaign，广告报表随即出现新广告。产品规则：提交后直接创建并启用，没有单独的激活步骤。

type UnitOutcome = "SUCCEEDED" | "RETRYABLE" | "UNKNOWN"

interface Unit {
  image?: DemoImageUnit
  unit_id: string
  drama: DemoDrama
  advertiser: DemoAdvertiser
  materials: DemoMaterial[]
  group_count: number
  ads_per_group: number
  /** 相对任务开始的完成时间（毫秒）；历史任务为 0。 */
  finish_at: number
  outcome: UnitOutcome
  /** 失败或待核实的原因码。 */
  error_code: string | null
  campaign: DemoLaunchedCampaign | null
  excluded_reason: string | null
}

interface Submission {
  submission_id: string
  preview_id: string
  draft_id: string
  batch_short_id: string
  bc_id: string
  started: number
  created_at: string
  actor_name: string
  provider_name: string
  strategy_label: string
  budget_strategy: "SERIES" | "ADGROUP"
  bid_strategy: "HIGHEST_VALUE" | "TARGET_ROAS"
  daily_budget: number
  units: Unit[]
  request_id: string | null
}

const submissions: Submission[] = []

const QUEUE_MS = 1200
const EXPAND_MS = 2000

function strategyLabel(versionId: string | undefined) {
  const strategy = DEMO_STRATEGIES.find(
    (s) => s.version_id === versionId || s.id === versionId,
  )
  return strategy ? `${strategy.name} · 当前版本` : DEMO_STRATEGIES[0].name
}

// ========== 状态推进 ==========

type UnitState = "QUEUED" | "RUNNING" | UnitOutcome | "EXCLUDED"

function imageId(unit: Unit, materialId: string) {
  return `demo-image-${unit.advertiser.advertiser_id}-${materialId}`
}

function unitState(sub: Submission, unit: Unit): UnitState {
  if (unit.excluded_reason) return "EXCLUDED"
  const elapsed = now() - sub.started
  if (elapsed < QUEUE_MS) return "QUEUED"
  if (elapsed < unit.finish_at) return "RUNNING"
  // 成功的单元在完成那一刻上线 Campaign（只做一次）。
  if (
    (unit.outcome === "SUCCEEDED" ||
      (unit.image && unit.outcome === "UNKNOWN")) &&
    !unit.campaign
  ) {
    unit.campaign =
      launchCampaigns({
        submissionId: sub.submission_id,
        dramaIds: [unit.drama.id],
        advertiserIds: [unit.advertiser.advertiser_id],
      })[0] ?? null
  }
  if (
    unit.image &&
    unit.campaign &&
    !unit.campaign.adgroups[0]?.ads[0]?.carousel_creatives
  ) {
    // 响应丢失仍保存同一远端模拟对象；核查只能读回，不重复创建。
    const campaign = unit.campaign
    campaign.name = unit.image.preview.campaign_name
    campaign.adgroups = [
      {
        adgroup_id: `demo-group-${unit.image.group.group_id}`,
        name: "模拟图片共享组",
        budget: sub.daily_budget,
        status: "ENABLE",
        ads: unit.image.ads.map((ad) => {
          const adId = `demo-ad-${ad.ad_id}`
          return {
            ad_id: adId,
            name: ad.name,
            material_id: "",
            status: "ENABLE" as const,
            carousel_creatives: (ad.creative_units ?? []).map((c) => ({
              advertiser_id: unit.advertiser.advertiser_id,
              ad_id: adId,
              remote_creative_id: null,
              ordered_image_ids: c.members.map(
                (m) =>
                  imageId(unit, m.material_id),
              ),
              music_id: c.music_id!,
            })),
            frozen_creative_links: [
              {
                submission_id: sub.submission_id,
                unit_id: unit.unit_id,
                group_id: unit.image!.group.group_id,
                planned_ad_id: ad.ad_id,
              },
            ],
          }
        }),
      },
    ]
  }
  return unit.outcome
}

/** 单元进度：0–1，用于“已创建”数量。 */
function unitProgress(sub: Submission, unit: Unit) {
  const state = unitState(sub, unit)
  if (state === "SUCCEEDED") return 1
  if (state === "QUEUED" || state === "EXCLUDED") return 0
  if (state === "RUNNING") {
    const start = unit.finish_at - 2500
    return Math.max(0, Math.min(0.95, (now() - sub.started - start) / 2500))
  }
  // 失败：系列已建、部分广告组失败；待核实：最后一步结果未知。
  return state === "RETRYABLE" ? 0.5 : 0.9
}

const structure = (unit: Unit) => ({
  campaign_count: 1,
  adgroup_count: unit.group_count,
  ad_count: unit.group_count * unit.ads_per_group,
})

function sum(units: Unit[], pick: (unit: Unit) => ObjectCounts): ObjectCounts {
  return units.reduce<Required<ObjectCounts>>(
    (acc, unit) => {
      const c = pick(unit)
      acc.campaign_count += c.campaign_count ?? 0
      acc.adgroup_count += c.adgroup_count ?? 0
      acc.ad_count += c.ad_count ?? 0
      return acc
    },
    { campaign_count: 0, adgroup_count: 0, ad_count: 0 },
  )
}

function created(sub: Submission, unit: Unit): ObjectCounts {
  const full = structure(unit)
  const p = unitProgress(sub, unit)
  if (p === 1) return full
  return {
    campaign_count: p > 0.1 ? 1 : 0,
    adgroup_count: Math.floor(full.adgroup_count * p),
    ad_count: Math.floor(full.ad_count * p),
  }
}

function submissionStatus(sub: Submission) {
  const states = sub.units
    .filter((u) => !u.excluded_reason)
    .map((u) => unitState(sub, u))
  if (states.every((s) => s === "QUEUED")) return "QUEUED"
  if (states.some((s) => s === "QUEUED" || s === "RUNNING")) return "RUNNING"
  if (states.some((s) => s === "UNKNOWN")) return "NEEDS_REVIEW"
  if (states.some((s) => s === "RETRYABLE"))
    return states.some((s) => s === "SUCCEEDED") ? "PARTIAL" : "FAILED"
  return "COMPLETED"
}

function counts(sub: Submission) {
  const active = sub.units.filter((u) => !u.excluded_reason)
  const byState = (state: UnitState) =>
    active.filter((u) => unitState(sub, u) === state)
  const succeeded = sum(active, (u) => created(sub, u))
  const failedUnits = byState("RETRYABLE")
  const unknownUnits = byState("UNKNOWN")
  const remaining = (u: Unit): ObjectCounts => {
    const full = structure(u)
    const done = created(sub, u)
    return {
      campaign_count: (full.campaign_count ?? 0) - (done.campaign_count ?? 0),
      adgroup_count: (full.adgroup_count ?? 0) - (done.adgroup_count ?? 0),
      ad_count: (full.ad_count ?? 0) - (done.ad_count ?? 0),
    }
  }
  return {
    planned: sum(sub.units, structure),
    submitted: sum(active, structure),
    succeeded,
    failed: sum(failedUnits, remaining),
    unknown: sum(unknownUnits, remaining),
    excluded: sum(
      sub.units.filter((u) => u.excluded_reason),
      structure,
    ),
    pending: sum(
      active.filter((u) => ["QUEUED", "RUNNING"].includes(unitState(sub, u))),
      remaining,
    ),
  }
}

function recovery(sub: Submission) {
  const retryable = sub.units.filter(
    (u) => unitState(sub, u) === "RETRYABLE",
  ).length
  const reconcilable = sub.units.filter(
    (u) => unitState(sub, u) === "UNKNOWN",
  ).length
  return {
    can_retry: retryable > 0,
    can_reconcile: reconcilable > 0,
    retryable_step_count: retryable,
    reconcilable_step_count: reconcilable,
    reasons: [],
  }
}

const route = (bcId: string) => ({
  connection_id: demoUuid(301),
  connection_name: "官方 API 搭建连接",
  channel: "OFFICIAL_API" as const,
  bc_id: bcId,
})

function view(sub: Submission): SubmissionView {
  const c = counts(sub)
  const stage = (state: UnitState) =>
    sub.units.filter((u) => unitState(sub, u) === state).length
  return {
    submission_id: sub.submission_id,
    preview_id: sub.preview_id,
    draft_id: sub.draft_id,
    batch_short_id: sub.batch_short_id,
    bc_id: sub.bc_id,
    status: submissionStatus(sub),
    expanded: now() - sub.started > EXPAND_MS,
    currency: "USD",
    daily_budget_sum: (sub.daily_budget * c.submitted.campaign_count!).toFixed(
      2,
    ),
    daily_budget_label:
      sub.budget_strategy === "SERIES" ? "系列预算合计" : "组预算合计",
    budget_strategy: sub.budget_strategy,
    bid_strategy: sub.bid_strategy,
    ...c,
    stage_counts: {
      QUEUED: stage("QUEUED"),
      RUNNING: stage("RUNNING"),
      SUCCEEDED: stage("SUCCEEDED"),
      RETRYABLE: stage("RETRYABLE"),
      UNKNOWN: stage("UNKNOWN"),
    },
    excluded_unit_count: sub.units.filter((u) => u.excluded_reason).length,
    drama_count: new Set(sub.units.map((u) => u.drama.id)).size,
    account_count: new Set(sub.units.map((u) => u.advertiser.advertiser_id))
      .size,
    created_at: sub.created_at,
    updated_at: isoAt(Math.min(now(), sub.started + lastFinish(sub))),
    execution_route: route(sub.bc_id),
    recovery_mode: "ORIGINAL_READ",
    error_code: null,
    recovery: recovery(sub),
    actor_name: sub.actor_name,
    provider_name: sub.provider_name,
    strategy_label: sub.strategy_label,
  }
}

const lastFinish = (sub: Submission) =>
  Math.max(QUEUE_MS, ...sub.units.map((u) => u.finish_at))

function listItem(sub: Submission): SubmissionListItem {
  const v = view(sub)
  return {
    submission_id: v.submission_id,
    batch_short_id: v.batch_short_id,
    bc_id: v.bc_id,
    status: v.status,
    created_at: v.created_at,
    updated_at: v.updated_at,
    drama_count: v.drama_count,
    account_count: v.account_count,
    excluded_unit_count: v.excluded_unit_count,
    submitted: v.submitted,
    succeeded: v.succeeded,
    failed: v.failed,
    unknown: v.unknown,
    budget_strategy: v.budget_strategy,
    bid_strategy: v.bid_strategy,
    actor_name: sub.actor_name,
    provider_name: sub.provider_name,
    strategy_label: sub.strategy_label,
  }
}

// ========== 步骤与明细 ==========

const STEP_STATUS: Record<UnitState, string> = {
  QUEUED: "QUEUED",
  RUNNING: "RUNNING",
  SUCCEEDED: "SUCCEEDED",
  RETRYABLE: "RETRYABLE",
  UNKNOWN: "UNKNOWN",
  EXCLUDED: "PENDING",
}

function step(
  sub: Submission,
  unit: Unit,
  kind: "MATERIAL" | "CAMPAIGN" | "ADGROUP" | "AD",
  extra: {
    id: string
    group_id?: string | null
    group_no?: number | null
    planned_ad_id?: string | null
    creative_no?: number | null
    material_id?: string | null
    remote_id?: string | null
    done: boolean
    failed?: boolean
    title: string
  },
): StepPublic {
  const state = unitState(sub, unit)
  const status = extra.done
    ? "SUCCEEDED"
    : extra.failed
      ? STEP_STATUS[state]
      : state === "SUCCEEDED"
        ? "SUCCEEDED"
        : state === "RETRYABLE" || state === "UNKNOWN"
          ? "PENDING"
          : STEP_STATUS[state]
  return {
    step_id: extra.id,
    unit_id: unit.unit_id,
    kind,
    title: extra.title,
    advertiser_id: unit.advertiser.advertiser_id,
    group_id: extra.group_id ?? null,
    group_no: extra.group_no ?? null,
    planned_ad_id: extra.planned_ad_id ?? null,
    creative_no: extra.creative_no ?? null,
    material_id: extra.material_id ?? null,
    status,
    remote_id: status === "SUCCEEDED" ? (extra.remote_id ?? null) : null,
    error_code:
      status === "RETRYABLE" || status === "UNKNOWN" ? unit.error_code : null,
    operation_status:
      status === "SUCCEEDED" && kind !== "MATERIAL" ? "ENABLE" : null,
    review_status:
      status === "SUCCEEDED" && kind !== "MATERIAL"
        ? unit.image
          ? "模拟回读"
          : "审核通过"
        : null,
    mismatch: false,
    checked_at:
      status === "SUCCEEDED" ? isoAt(sub.started + unit.finish_at) : null,
    can_historical_read: false,
  }
}

const unitStepId = (unit: Unit, suffix: string) =>
  `${unit.unit_id.slice(0, 24)}${suffix.padStart(12, "0").slice(-12)}`

function campaignStep(sub: Submission, unit: Unit) {
  const p = unitProgress(sub, unit)
  return step(sub, unit, "CAMPAIGN", {
    id: unitStepId(unit, "c"),
    remote_id:
      unit.campaign?.campaign_id ??
      `17${unit.unit_id.replace(/\D/g, "").slice(-14)}`,
    done: p > 0.1,
    title: `${unit.drama.provider_kind}-${unit.drama.title}-${unit.drama.external_id}`,
  })
}

function groups(sub: Submission, unit: Unit): SubmissionGroupPublic[] {
  const p = unitProgress(sub, unit)
  return Array.from({ length: unit.group_count }, (_, g) => {
    const groupId = unit.image?.group.group_id ?? unitStepId(unit, `g${g}`)
    const done = (g + 1) / unit.group_count <= p
    const materials = unit.materials.filter(
      (_, i) => i % unit.group_count === g,
    )
    return {
      group_id: groupId,
      unit_id: unit.unit_id,
      group_no: g + 1,
      name: `${unit.drama.external_id}-组${g + 1}`,
      material_count: materials.length,
      ad_count: unit.ads_per_group,
      step: step(sub, unit, "ADGROUP", {
        id: unitStepId(unit, `sg${g}`),
        group_id: groupId,
        group_no: g + 1,
        remote_id:
          unit.campaign?.adgroups[g]?.adgroup_id ??
          `18${unit.unit_id.replace(/\D/g, "").slice(-12)}${g}`,
        done,
        failed: !done && g === unit.group_count - 1,
        title: `广告组 ${g + 1}`,
      }),
    }
  })
}

const COPY = [
  "She married a stranger to save her family. He turned out to be a billionaire.",
  "Watch the full story — the twist in episode 5 will shock you.",
  "Everyone underestimated her. Big mistake.",
]

function ads(
  sub: Submission,
  unit: Unit,
  groupId: string,
): SubmissionAdPublic[] {
  const group = groups(sub, unit).find((g) => g.group_id === groupId)
  if (!group) return []
  return Array.from({ length: unit.ads_per_group }, (_, a) => ({
    planned_ad_id:
      unit.image?.ads[a]?.ad_id ?? unitStepId(unit, `a${group.group_no}${a}`),
    group_id: groupId,
    creative_no: a + 1,
    name: `${unit.drama.external_id}-组${group.group_no}-创意${a + 1}`,
    text: COPY[(group.group_no + a) % COPY.length],
    cta_option_ids: ["WATCH_NOW"],
    step: step(sub, unit, "AD", {
      id: unitStepId(unit, `sa${group.group_no}${a}`),
      group_id: groupId,
      group_no: group.group_no,
      planned_ad_id:
        unit.image?.ads[a]?.ad_id ?? unitStepId(unit, `a${group.group_no}${a}`),
      creative_no: a + 1,
      remote_id: unit.image
        ? `demo-ad-${unit.image.ads[a].ad_id}`
        : `19${unit.unit_id.replace(/\D/g, "").slice(-11)}${group.group_no}${a}`,
      done: group.step?.status === "SUCCEEDED",
      title: `广告 ${a + 1}`,
    }),
  }))
}

function unitMaterials(sub: Submission, unit: Unit, groupId: string) {
  const group = groups(sub, unit).find((g) => g.group_id === groupId)
  if (!group) return []
  return unit.materials
    .filter((_, i) => i % unit.group_count === group.group_no - 1)
    .map(
      (material, position): SubmissionMaterialPublic => ({
        material_id: material.id,
        position: position + 1,
        file_name: material.file_name,
        preview_available: true,
        video_id: unit.image ? null : material.video_id,
        image_id: unit.image
          ? imageId(unit, material.id)
          : null,
        step: step(sub, unit, "MATERIAL", {
          id: unitStepId(unit, `m${material.index}`),
          material_id: material.id,
          remote_id: unit.image ? imageId(unit, material.id) : material.video_id,
          done: unitState(sub, unit) !== "QUEUED",
          title: material.file_name,
        }),
      }),
    )
}

function unitView(sub: Submission, unit: Unit): SubmissionUnitPublic {
  const state = unitState(sub, unit)
  const full = structure(unit)
  const done = created(sub, unit)
  const excluded = !!unit.excluded_reason
  return {
    unit_id: unit.unit_id,
    drama_id: unit.drama.id,
    title: unit.drama.title,
    advertiser_id: unit.advertiser.advertiser_id,
    account_name: unit.advertiser.name,
    campaign_name: `${unit.drama.provider_kind}-${unit.drama.title}-${unit.drama.external_id}`,
    disposition: excluded ? "EXCLUDED" : "SUBMITTED",
    reason_codes: unit.excluded_reason ? [unit.excluded_reason] : [],
    expanded: !excluded && now() - sub.started > EXPAND_MS,
    result_status:
      excluded || state === "QUEUED"
        ? null
        : state === "RETRYABLE"
          ? "PARTIAL"
          : state === "UNKNOWN"
            ? "NEEDS_REVIEW"
            : state === "SUCCEEDED"
              ? "COMPLETED"
              : "RUNNING",
    group_count: full.adgroup_count,
    ad_count: full.ad_count,
    succeeded_group_count: done.adgroup_count,
    succeeded_ad_count: done.ad_count,
    material_count: unit.materials.length,
    ready_material_count: state === "QUEUED" ? 0 : unit.materials.length,
    unique_material_count: unit.materials.length,
    material_allocation_count: unit.materials.length,
    structure_summary: `${unit.group_count} 个广告组 · 每组 ${unit.ads_per_group} 个广告`,
    campaign_step: excluded ? null : campaignStep(sub, unit),
  }
}

function unitSteps(sub: Submission, unit: Unit): StepPublic[] {
  if (unit.excluded_reason) return []
  const groupList = groups(sub, unit)
  return [
    ...unit.materials.map((m) =>
      step(sub, unit, "MATERIAL", {
        id: unitStepId(unit, `m${m.index}`),
        material_id: m.id,
        remote_id: unit.image ? imageId(unit, m.id) : m.video_id,
        done: unitState(sub, unit) !== "QUEUED",
        title: m.file_name,
      }),
    ),
    campaignStep(sub, unit),
    ...groupList.map((g) => g.step as StepPublic),
    ...groupList.flatMap((g) =>
      ads(sub, unit, g.group_id).map((a) => a.step as StepPublic),
    ),
  ]
}

// ========== 单元构建 ==========

function makeUnits(
  dramas: DemoDrama[],
  advertisers: DemoAdvertiser[],
  groupCount: number,
  adsPerGroup: number,
  stepMs: number,
): Unit[] {
  const units: Unit[] = []
  for (const drama of dramas)
    for (const advertiser of advertisers) {
      const index = units.length
      units.push({
        unit_id: demoUuid(),
        drama,
        advertiser,
        materials: materialsOfDrama(drama.id),
        group_count: groupCount,
        ads_per_group: adsPerGroup,
        finish_at: QUEUE_MS + 2500 + index * stepMs,
        outcome: "SUCCEEDED",
        error_code: null,
        campaign: null,
        excluded_reason: null,
      })
    }
  return units
}

function resolveDramas(lines: string[]) {
  return lines
    .map((line) => line.trim().toLowerCase())
    .filter(Boolean)
    .map((line) =>
      DEMO_DRAMAS.find(
        (d) =>
          d.title.toLowerCase() === line ||
          d.title_zh === line ||
          d.external_id.toLowerCase() === line,
      ),
    )
    .filter((d): d is DemoDrama => !!d)
}

function resolveAccounts(lines: string[], bcId: string) {
  const seen = new Set<string>()
  return lines
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) =>
      DEMO_ADVERTISERS.find(
        (a) =>
          a.bc_id === bcId && (a.advertiser_id === line || a.name === line),
      ),
    )
    .filter((a): a is DemoAdvertiser => {
      if (!a || seen.has(a.advertiser_id)) return false
      seen.add(a.advertiser_id)
      return true
    })
}

// ========== 接口 ==========

handle("builds-submit_preview", ({ path, body }) => {
  const preview = demoPreviews.find((p) => p.preview_id === path.preview_id)
  if (!preview) return demoError(404, "preview_not_found", "预览不存在")
  // 同一预览只能提交一次；重复提交返回首次的任务。
  const existing = submissions.find((s) => s.preview_id === preview.preview_id)
  if (existing)
    return new DemoReply(202, {
      submission_id: existing.submission_id,
      status: view(existing).status,
    })
  const draft = demoDrafts.find((d) => d.draft_id === preview.draft_id)
  const dramas = resolveDramas(draft?.drama_lines ?? [])
  const accounts = resolveAccounts(draft?.account_lines ?? [], preview.bc_id)
  const unitCount = Math.max(1, dramas.length * accounts.length)
  const groupCount = Math.max(1, Math.round(preview.adgroup_count / unitCount))
  const adsPerGroup = Math.max(
    1,
    Math.round(preview.ad_count / Math.max(1, preview.adgroup_count)),
  )
  const sub: Submission = {
    submission_id: demoUuid(),
    preview_id: preview.preview_id,
    draft_id: preview.draft_id,
    batch_short_id: `B${String(now()).slice(-7, -1)}`,
    bc_id: preview.bc_id,
    started: now(),
    created_at: isoAt(now()),
    actor_name: DEMO_USER.full_name,
    provider_name:
      DEMO_PROVIDERS.find((p) => p.id === draft?.provider_connection_id)
        ?.display_name ?? DEMO_PROVIDERS[0].display_name,
    strategy_label: strategyLabel(draft?.strategy_version_id),
    budget_strategy:
      preview.budget_strategy === "ADGROUP" ? "ADGROUP" : "SERIES",
    bid_strategy:
      preview.bid_strategy === "HIGHEST_VALUE"
        ? "HIGHEST_VALUE"
        : "TARGET_ROAS",
    daily_budget:
      Number(preview.daily_budget_sum) / Math.max(1, preview.campaign_count) ||
      100,
    units: makeUnits(dramas, accounts, groupCount, adsPerGroup, 1600),
    request_id: body.request_id,
  }
  const imageUnits = imageDeliveries.get(preview.preview_id)
  if (imageUnits) {
    sub.units = sub.units.map((unit, index) => {
      const image = imageUnits.find(
        (i) =>
          i.preview.drama_id === unit.drama.id &&
          i.preview.advertiser_id === unit.advertiser.advertiser_id,
      )!
      const members = [
        ...new Map(
          image.ads
            .flatMap((a) => (a.creative_units ?? []).flatMap((c) => c.members))
            .map((m) => [m.material_id, m]),
        ).values(),
      ]
      return {
        ...unit,
        image,
        unit_id: image.preview.unit_id,
        group_count: 1,
        ads_per_group: image.ads.length,
        materials: members.map((m, i) => ({
          id: m.material_id,
          drama_id: unit.drama.id,
          index: i,
          file_name: m.file_name,
          duration_seconds: 0,
          size_bytes: 1024,
          advertiser_id: unit.advertiser.advertiser_id,
          video_id: "",
          uploaded_at: sub.created_at,
          cover_url: "",
        })),
        outcome: index === 0 ? ("UNKNOWN" as const) : ("SUCCEEDED" as const),
        error_code: index === 0 ? "tiktok_call_deadline_exceeded" : null,
      }
    })
  }
  preview.submission_id = sub.submission_id
  submissions.unshift(sub)
  return new DemoReply(202, {
    submission_id: sub.submission_id,
    status: "QUEUED",
  })
})

handle("builds-saved_submission", ({ path }) => {
  const sub = submissions.find((s) => s.request_id === path.request_id)
  return sub
    ? { submission_id: sub.submission_id, status: view(sub).status }
    : demoError(404, "request_not_found", "提交请求不存在")
})

const STATUS_GROUPS: Record<string, string[]> = {
  active: ["QUEUED", "RUNNING"],
  attention: ["PARTIAL", "FAILED", "NEEDS_REVIEW"],
  completed: ["COMPLETED"],
}

handle("builds-list_submissions", ({ query }) => {
  const group = query.status_group && STATUS_GROUPS[query.status_group]
  const items = submissions
    .filter((s) => s.bc_id === query.bc_id)
    .map(listItem)
    .filter((item) => !group || group.includes(item.status))
    .filter(
      (item) =>
        !query.q ||
        [item.batch_short_id, item.submission_id, item.strategy_label]
          .join(" ")
          .toLowerCase()
          .includes(query.q.toLowerCase()) ||
        submissions
          .find((s) => s.submission_id === item.submission_id)
          ?.units.some((u) =>
            u.drama.title.toLowerCase().includes((query.q ?? "").toLowerCase()),
          ),
    )
  return paginate(items, query, (item) => item.submission_id)
})

const find = (id: string) => submissions.find((s) => s.submission_id === id)
const notFound = () => demoError(404, "submission_not_found", "搭建任务不存在")

handle("builds-get_submission", ({ path }) => {
  const sub = find(path.submission_id)
  return sub ? view(sub) : notFound()
})

function filterUnits(
  sub: Submission,
  query: { advertiser_id?: string | null; drama_id?: string | null },
) {
  return sub.units.filter(
    (u) =>
      (!query.advertiser_id ||
        u.advertiser.advertiser_id === query.advertiser_id) &&
      (!query.drama_id || u.drama.id === query.drama_id),
  )
}

handle("builds-get_submission_units", ({ path, query }) => {
  const sub = find(path.submission_id)
  if (!sub) return notFound()
  return paginate(
    filterUnits(sub, query)
      .filter((u) => !u.excluded_reason)
      .map((u) => unitView(sub, u)),
    query,
    (u) => u.unit_id,
  )
})

handle("builds-get_submission_excluded", ({ path, query }) => {
  const sub = find(path.submission_id)
  if (!sub) return notFound()
  return paginate(
    filterUnits(sub, query)
      .filter((u) => u.excluded_reason)
      .map((u) => unitView(sub, u)),
    query,
    (u) => u.unit_id,
  )
})

// “异常与待核实”页签按结果筛选步骤：失败、待核实等。
const RESULT_FILTER: Record<string, string[]> = {
  attention: ["RETRYABLE", "UNKNOWN", "FAILED"],
  failed: ["RETRYABLE", "FAILED"],
  unknown: ["UNKNOWN"],
  succeeded: ["SUCCEEDED"],
}

handle("builds-get_submission_steps", ({ path, query }) => {
  const sub = find(path.submission_id)
  if (!sub) return notFound()
  const wanted = query.result
    ? (RESULT_FILTER[query.result] ?? [query.result.toUpperCase()])
    : null
  const steps = filterUnits(sub, query)
    .flatMap((u) => unitSteps(sub, u))
    .filter((s) => !query.kind || s.kind === query.kind)
    .filter((s) => !wanted || wanted.includes(s.status))
  return paginate(steps, query, (s) => s.step_id)
})

handle("builds-get_submission_groups", ({ path, query }) => {
  const sub = find(path.submission_id)
  const unit = sub?.units.find((u) => u.unit_id === path.unit_id)
  if (!sub || !unit) return notFound()
  return paginate(groups(sub, unit), query, (g) => g.group_id)
})

handle("builds-get_submission_ads", ({ path, query }) => {
  const sub = find(path.submission_id)
  const unit = sub?.units.find((u) => u.unit_id === path.unit_id)
  if (!sub || !unit) return notFound()
  return paginate(ads(sub, unit, path.group_id), query, (a) => a.planned_ad_id)
})

handle("builds-get_submission_materials", ({ path, query }) => {
  const sub = find(path.submission_id)
  const unit = sub?.units.find((u) => u.unit_id === path.unit_id)
  if (!sub || !unit) return notFound()
  return paginate(
    unitMaterials(sub, unit, path.group_id),
    query,
    (m) => m.material_id,
  )
})

handle("builds-get_submission_events", ({ path, query }) => {
  const sub = find(path.submission_id)
  if (!sub) return notFound()
  const events: SubmissionEventPublic[] = []
  for (const unit of sub.units) {
    const state = unitState(sub, unit)
    if (["QUEUED", "RUNNING", "EXCLUDED"].includes(state)) continue
    events.push({
      evidence_id: unitStepId(unit, "e"),
      step_id: unitStepId(unit, "c"),
      attempt: 1,
      conclusion:
        state === "SUCCEEDED"
          ? "SUCCEEDED"
          : state === "UNKNOWN"
            ? "UNKNOWN"
            : "FAILED",
      observed_at: isoAt(sub.started + unit.finish_at),
      unit_id: unit.unit_id,
      kind: "CAMPAIGN",
      title:
        state === "SUCCEEDED"
          ? `${unit.drama.title} 创建完成并已启用`
          : state === "UNKNOWN"
            ? `${unit.drama.title} 最后一个广告组结果待核实`
            : `${unit.drama.title} 部分广告组创建失败，可重试`,
      advertiser_id: unit.advertiser.advertiser_id,
      account_name: unit.advertiser.name,
    })
  }
  events.sort((a, b) => b.observed_at.localeCompare(a.observed_at))
  return paginate(events, query, (e) => e.evidence_id)
})

// ========== 失败重试与核查 ==========

interface StoredRecovery {
  receipt: RecoveryReceipt
  started: number
  units: Unit[]
}
const recoveries = new Map<string, StoredRecovery>()

function startRecovery(
  submissionId: string,
  requestId: string,
  kind: "RETRY" | "RECONCILE",
) {
  const sub = find(submissionId)
  if (!sub) return notFound()
  const existing = [...recoveries.values()].find(
    (r) => r.receipt.request_id === requestId,
  )
  if (existing) return new DemoReply(202, recoveryView(existing))
  const target = kind === "RETRY" ? "RETRYABLE" : "UNKNOWN"
  const units = sub.units.filter((u) => unitState(sub, u) === target)
  const stored: StoredRecovery = {
    receipt: {
      recovery_id: demoUuid(),
      request_id: requestId,
      submission_id: submissionId,
      kind,
      state: "QUEUED",
      scheduled_count: units.length,
    },
    started: now(),
    units,
  }
  recoveries.set(stored.receipt.recovery_id, stored)
  return new DemoReply(202, recoveryView(stored))
}

/** 重试或核查约 3 秒后完成：对应单元变为成功（核查确认已创建，重试补建成功）。 */
function recoveryView(stored: StoredRecovery): RecoveryReceipt {
  const elapsed = now() - stored.started
  const state =
    elapsed < 800 ? "QUEUED" : elapsed < 3000 ? "RUNNING" : "COMPLETED"
  if (state === "COMPLETED" && stored.receipt.state !== "COMPLETED") {
    const sub = find(stored.receipt.submission_id)
    for (const unit of stored.units) {
      unit.outcome = "SUCCEEDED"
      unit.error_code = null
      if (sub && unit.image) unitState(sub, unit)
      else if (sub && !unit.campaign)
        unit.campaign =
          launchCampaigns({
            submissionId: sub.submission_id,
            dramaIds: [unit.drama.id],
            advertiserIds: [unit.advertiser.advertiser_id],
          })[0] ?? null
    }
  }
  stored.receipt = { ...stored.receipt, state }
  return stored.receipt
}

handle("builds-retry_submission", ({ path, body }) =>
  startRecovery(path.submission_id, body.request_id, "RETRY"),
)

handle("builds-reconcile_submission", ({ path, body }) =>
  startRecovery(path.submission_id, body.request_id, "RECONCILE"),
)

handle("builds-get_submission_recovery", ({ path }) => {
  const stored = recoveries.get(path.recovery_id)
  return stored
    ? recoveryView(stored)
    : demoError(404, "recovery_not_found", "恢复记录不存在")
})

handle("builds-saved_submission_recovery", ({ path }) => {
  const stored = [...recoveries.values()].find(
    (r) => r.receipt.request_id === path.request_id,
  )
  return stored
    ? recoveryView(stored)
    : demoError(404, "recovery_not_found", "恢复记录不存在")
})

// ========== 预置历史任务 ==========
// 三次历史提交：全部成功、部分失败（可重试）、有一项待核实（可核查）。
// 已上线的 Campaign 与 demoWorld.launched 中同一 submission_id 的记录一一对应。

function seedHistory(
  submissionId: string,
  daysAgo: number,
  actor: string,
  strategyIndex: number,
  extra: (dramas: DemoDrama[], accounts: DemoAdvertiser[]) => Unit[],
) {
  const launched = demoWorld.launched.filter(
    (c) => c.submission_id === submissionId,
  )
  const units: Unit[] = launched.map((campaign) => {
    const drama = DEMO_DRAMAS.find(
      (d) => d.id === campaign.drama_id,
    ) as DemoDrama
    const advertiser = DEMO_ADVERTISERS.find(
      (a) => a.advertiser_id === campaign.advertiser_id,
    ) as DemoAdvertiser
    return {
      unit_id: demoUuid(),
      drama,
      advertiser,
      materials: materialsOfDrama(drama.id),
      group_count: campaign.adgroups.length,
      ads_per_group: Math.max(
        1,
        Math.round(
          campaign.adgroups.reduce((n, g) => n + g.ads.length, 0) /
            Math.max(1, campaign.adgroups.length),
        ),
      ),
      finish_at: 0,
      outcome: "SUCCEEDED",
      error_code: null,
      campaign,
      excluded_reason: null,
    }
  })
  const dramas = [...new Set(units.map((u) => u.drama))]
  const accounts = [...new Set(units.map((u) => u.advertiser))]
  units.push(...extra(dramas, accounts))
  const strategy = DEMO_STRATEGIES[strategyIndex]
  const started = Date.parse(isoDaysAgo(daysAgo, 10))
  submissions.push({
    submission_id: submissionId,
    preview_id: demoUuid(),
    draft_id: demoUuid(),
    batch_short_id: `B${submissionId.replace(/\D/g, "").slice(-6)}`,
    bc_id: MAIN_BC_ID,
    started,
    created_at: isoAt(started),
    actor_name: actor,
    provider_name:
      DEMO_PROVIDERS[strategyIndex % DEMO_PROVIDERS.length].display_name,
    strategy_label: `${strategy.name} · v${strategyIndex + 1}`,
    budget_strategy: strategyIndex === 3 ? "ADGROUP" : "SERIES",
    bid_strategy: strategyIndex % 2 === 0 ? "TARGET_ROAS" : "HIGHEST_VALUE",
    daily_budget: launched[0]?.budget ?? 200,
    units,
    request_id: null,
  })
}

const historyUnit = (
  drama: DemoDrama,
  advertiser: DemoAdvertiser,
  outcome: UnitOutcome,
  errorCode: string | null,
  excluded: string | null = null,
): Unit => ({
  unit_id: demoUuid(),
  drama,
  advertiser,
  materials: materialsOfDrama(drama.id),
  group_count: 3,
  ads_per_group: 2,
  finish_at: 0,
  outcome,
  error_code: errorCode,
  campaign: null,
  excluded_reason: excluded,
})

const accountsPool = deliveryAdvertisers()
// 最近一次（3 天前）：全部成功，另有一个账户因缺少搭建权限被排除。
seedHistory(HISTORY_SUBMISSION_IDS[2], 3, "周航", 2, (dramas) => [
  historyUnit(
    dramas[0],
    accountsPool[5],
    "SUCCEEDED",
    null,
    "account_access_denied",
  ),
])
// 7 天前：两个组合因平台预算校验失败，可一键重试补建。
seedHistory(HISTORY_SUBMISSION_IDS[1], 7, "陈曦", 1, (_dramas, accounts) => [
  historyUnit(DEMO_DRAMAS[9], accounts[0], "RETRYABLE", "budget_out_of_range"),
  historyUnit(
    DEMO_DRAMAS[9],
    accounts[1] ?? accounts[0],
    "RETRYABLE",
    "budget_out_of_range",
  ),
])
// 14 天前：一个组合的最后一步平台响应超时，结果待核实，可核查确认。
seedHistory(
  HISTORY_SUBMISSION_IDS[0],
  14,
  DEMO_TEAM[0].full_name,
  0,
  (_dramas, accounts) => [
    historyUnit(
      DEMO_DRAMAS[10],
      accounts[0],
      "UNKNOWN",
      "tiktok_call_deadline_exceeded",
    ),
  ],
)
submissions.sort((a, b) => b.started - a.started)
