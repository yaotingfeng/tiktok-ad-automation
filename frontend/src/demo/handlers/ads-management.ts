import type {
  EntityRef,
  FrozenSelection,
  FrozenTikTokRoute,
  ManagementCounts,
  ManagementItemPublic,
  ManagementPreviewPublic,
  ManagementTaskPublic,
  MaterialUseRef,
  MutationSpec,
} from "@/client"
import {
  DEMO_MATERIALS,
  DEMO_USER,
  type DemoLaunchedAd,
  type DemoLaunchedAdGroup,
  type DemoLaunchedCampaign,
  demoWorld,
  MAIN_BC_ID,
  MAIN_TENANT_ID,
} from "../fixtures/world"
import {
  demoUuid,
  isoAt,
  isoDaysAgo,
  nextId,
  now,
  stageAt,
} from "../runtime/clock"
import { paginate } from "../runtime/paging"
import { DemoReply, demoError, handle } from "../runtime/registry"

// 体验版假后端：广告管理（报表勾选 → 冻结选择 → 直接提交 → 后台准备 → 执行 → 操作记录）。
// 所有修改都落到共享的 demoWorld.launched，广告报表随之变化。
// 业务规则与真实系统一致：
// - 预算改在持有预算的那一层：演示数据里系列使用系列预算（CBO），选广告组时改广告组预算；
// - 目标 ROAS 改在广告组上，选系列时展开到它下面的广告组；
// - 改名只支持系列；开关状态支持系列、广告组、广告和素材。

type Found =
  | { kind: "campaign"; campaign: DemoLaunchedCampaign }
  | {
      kind: "adgroup"
      campaign: DemoLaunchedCampaign
      adgroup: DemoLaunchedAdGroup
    }
  | {
      kind: "ad"
      campaign: DemoLaunchedCampaign
      adgroup: DemoLaunchedAdGroup
      ad: DemoLaunchedAd
    }

function findObject(remoteId: string): Found | null {
  for (const campaign of demoWorld.launched) {
    if (campaign.campaign_id === remoteId) return { kind: "campaign", campaign }
    for (const adgroup of campaign.adgroups) {
      if (adgroup.adgroup_id === remoteId)
        return { kind: "adgroup", campaign, adgroup }
      for (const ad of adgroup.ads)
        if (ad.ad_id === remoteId) return { kind: "ad", campaign, adgroup, ad }
    }
  }
  return null
}

const refOf = (tenantId: string, found: Found): EntityRef => ({
  tenant_id: tenantId,
  advertiser_id: found.campaign.advertiser_id,
  kind: found.kind,
  remote_id:
    found.kind === "campaign"
      ? found.campaign.campaign_id
      : found.kind === "adgroup"
        ? found.adgroup.adgroup_id
        : found.ad.ad_id,
})

const nameOf = (found: Found) =>
  found.kind === "campaign"
    ? found.campaign.name
    : found.kind === "adgroup"
      ? found.adgroup.name
      : found.ad.name

// 广告组目标 ROAS：演示数据只在系列上记录策略的目标 ROAS，广告组未单独修改时沿用它。
// 最高价值出价的系列没有目标 ROAS，此时预览提示“平台未返回目标 ROAS”。
const adgroupRoas = new Map<string, number>()
const roasOf = (campaign: DemoLaunchedCampaign, adgroup: DemoLaunchedAdGroup) =>
  adgroupRoas.get(adgroup.adgroup_id) ?? campaign.target_roas

// 素材开关状态（Smart+ 广告里单条素材），默认启用。
const materialStatus = new Map<string, "ENABLE" | "DISABLE">()
const materialKey = (use: MaterialUseRef) =>
  `${use.ad_ref.remote_id}:${use.platform_material_id}`

// ========== 冻结选择 ==========

interface StoredSelection {
  refs: EntityRef[]
  materialUses: MaterialUseRef[]
}
const selections = new Map<
  string,
  StoredSelection & { tenantId: string; bcId: string; expiresAt: number }
>()

/** 报表行键：`${bc}-${对象ID}`；账户、剧、素材行另有前缀，不能直接管理。 */
function remoteIdOfRowKey(rowKey: string) {
  const parts = rowKey.split("-")
  if (parts.length !== 2) return null
  return parts[1]
}

function frozen(
  tenantId: string,
  stored: StoredSelection,
  bcId: string,
): FrozenSelection {
  const selectionId = demoUuid()
  const expiresAt = now() + 30 * 60 * 1000
  selections.set(selectionId, { ...stored, tenantId, bcId, expiresAt })
  return {
    selection_id: selectionId,
    snapshot_id: demoUuid(9001),
    refs: stored.refs,
    material_uses: stored.materialUses,
    membership_digest:
      `${tenantId.slice(0, 8)}${selectionId.slice(-12)}`.padEnd(64, "0"),
    expires_at: isoAt(expiresAt),
  }
}

handle("ads_reporting-freeze_ad_selection", ({ path, query, body }) => {
  // “选择全部匹配结果”时前端只给排除列表；演示数据量小，按全部已上线系列处理。
  const keys =
    body.mode === "ALL_MATCHING"
      ? demoWorld.launched
          .filter((c) => c.bc_id === query.bc_id)
          .map((c) => `${query.bc_id}-${c.campaign_id}`)
          .filter((key) => !(body.excluded_row_keys ?? []).includes(key))
      : (body.row_keys ?? [])
  const refs = keys
    .map(remoteIdOfRowKey)
    .map((id) => (id ? findObject(id) : null))
    .filter((found): found is Found => !!found)
    .map((found) => refOf(path.tenant_id, found))
  return frozen(path.tenant_id, { refs, materialUses: [] }, query.bc_id)
})

handle(
  "ads_reporting-freeze_ad_material_selection",
  ({ path, query, body }) => {
    const found = findObject(path.remote_id)
    const adRef: EntityRef = found
      ? refOf(path.tenant_id, found)
      : {
          tenant_id: path.tenant_id,
          advertiser_id: query.advertiser_id,
          kind: "ad",
          remote_id: path.remote_id,
        }
    const materialUses = body.ad_material_ids.map((id) => {
      const material = DEMO_MATERIALS.find((m) => m.id === id)
      return {
        ad_ref: adRef,
        platform_material_id: material?.video_id ?? id,
        ad_material_id: id,
        material_type: "VIDEO",
      }
    })
    return frozen(path.tenant_id, { refs: [], materialUses }, query.bc_id)
  },
)

handle("ad_management-resolve_rename_selection", ({ path, body }) => {
  const seen = new Set<string>()
  const problems: {
    campaign_id: string
    new_name: string
    reason: "id_invalid" | "not_found" | "duplicate_campaign_id"
  }[] = []
  const refs: EntityRef[] = []
  for (const row of body.rows) {
    if (!/^\d+$/.test(row.campaign_id)) {
      problems.push({ ...row, reason: "id_invalid" })
      continue
    }
    if (seen.has(row.campaign_id)) {
      problems.push({ ...row, reason: "duplicate_campaign_id" })
      continue
    }
    seen.add(row.campaign_id)
    const found = findObject(row.campaign_id)
    if (found?.kind !== "campaign") {
      problems.push({ ...row, reason: "not_found" })
      continue
    }
    refs.push(refOf(path.tenant_id, found))
  }
  return new DemoReply(201, {
    selection: refs.length
      ? frozen(path.tenant_id, { refs, materialUses: [] }, body.bc_id)
      : null,
    problems,
  })
})

// ========== 预览 ==========

const route = (tenantId: string, bcId: string): FrozenTikTokRoute => ({
  tenant_id: tenantId,
  bc_id: bcId,
  connection_id: demoUuid(501),
  channel: "OFFICIAL_API",
  authorization_revision: 1,
  binding_revision: 1,
  adapter_contract_revision: "demo-v1",
})

/** 按调整方式计算新值；结果不大于 0 时返回 null（预览提示“调整后的数值不大于 0”）。 */
function adjust(original: number, mutation: MutationSpec) {
  const value = Number(mutation.value ?? 0)
  let next = value
  switch (mutation.mode) {
    case "add":
      next = original + value
      break
    case "subtract":
      next = original - value
      break
    case "increase_percent":
      next = original * (1 + value / 100)
      break
    case "decrease_percent":
      next = original * (1 - value / 100)
      break
  }
  if (mutation.min_value != null)
    next = Math.max(next, Number(mutation.min_value))
  if (mutation.max_value != null)
    next = Math.min(next, Number(mutation.max_value))
  next = Math.round(next * 100) / 100
  return next > 0 ? next : null
}

const money = (value: number) => value.toFixed(2)

function renamed(name: string, mutation: MutationSpec, remoteId: string) {
  const rule = mutation.rename
  if (!rule) return name
  if (rule.kind === "explicit") return rule.names?.[remoteId] ?? name
  if (rule.kind === "affix")
    return `${rule.prefix ?? ""}${name}${rule.suffix ?? ""}`
  return rule.find ? name.split(rule.find).join(rule.replace_with ?? "") : name
}

interface PlannedItem {
  item: ManagementItemPublic
  /** 执行成功时把修改写回演示数据。 */
  apply?: () => void
}

function plan(
  tenantId: string,
  selection: StoredSelection,
  mutation: MutationSpec,
): PlannedItem[] {
  const excluded = new Set(
    (mutation.excluded_refs ?? []).map((ref) => ref.remote_id),
  )
  const planned: PlannedItem[] = []
  const pending = (
    ref: EntityRef,
    name: string,
    original: string | null,
    final: string,
    apply: () => void,
  ): PlannedItem =>
    original === final
      ? {
          item: {
            ref,
            name,
            original_value: original,
            final_value: final,
            reason: "no_change",
            execution_result: "NO_CHANGE",
          },
        }
      : {
          item: {
            ref,
            name,
            original_value: original,
            final_value: final,
            execution_result: "PENDING",
          },
          apply,
        }
  const info = (
    ref: EntityRef,
    name: string,
    reason: string,
    original: string | null = null,
  ): PlannedItem => ({
    item: {
      ref,
      name,
      original_value: original,
      reason,
      execution_result: reason.startsWith("excluded")
        ? "NOT_SENT"
        : "UNSUPPORTED",
    },
  })

  for (const use of selection.materialUses) {
    const key = materialKey(use)
    const original = materialStatus.get(key) ?? "ENABLE"
    const final = String(mutation.value)
    const found = findObject(use.ad_ref.remote_id)
    const material = DEMO_MATERIALS.find((m) => m.id === use.ad_material_id)
    const entry = pending(
      use.ad_ref,
      material?.file_name ?? (found ? nameOf(found) : use.platform_material_id),
      original,
      final,
      () => materialStatus.set(key, final === "ENABLE" ? "ENABLE" : "DISABLE"),
    )
    entry.item.material_use = use
    planned.push(entry)
  }

  for (const ref of selection.refs) {
    const found = findObject(ref.remote_id)
    if (!found) {
      planned.push(info(ref, ref.remote_id, "directory_missing"))
      continue
    }
    const name = nameOf(found)
    if (excluded.has(ref.remote_id)) {
      planned.push(info(ref, name, "excluded_ref"))
      continue
    }
    switch (mutation.field) {
      case "status": {
        const target =
          found.kind === "campaign"
            ? found.campaign
            : found.kind === "adgroup"
              ? found.adgroup
              : found.ad
        const final = String(mutation.value)
        planned.push(
          pending(ref, name, target.status, final, () => {
            target.status = final === "ENABLE" ? "ENABLE" : "DISABLE"
          }),
        )
        break
      }
      case "budget": {
        if (found.kind === "ad") {
          planned.push(info(ref, name, "budget_owner_missing"))
          break
        }
        const owner = found.kind === "campaign" ? found.campaign : found.adgroup
        const next = adjust(owner.budget, mutation)
        if (next === null) {
          planned.push(
            info(ref, name, "final_value_invalid", money(owner.budget)),
          )
          break
        }
        planned.push(
          pending(ref, name, money(owner.budget), money(next), () => {
            owner.budget = next
          }),
        )
        break
      }
      case "roas": {
        // 目标 ROAS 在广告组上：选中系列时，系列本身作为“关联影响”展示，实际修改各广告组。
        const groups =
          found.kind === "campaign"
            ? found.campaign.adgroups
            : found.kind === "adgroup"
              ? [found.adgroup]
              : [found.adgroup]
        if (found.kind !== "adgroup")
          planned.push({
            item: {
              ref,
              name,
              reason: "linked_impact",
              execution_result: "NO_CHANGE",
            },
          })
        for (const adgroup of groups) {
          const groupRef = refOf(tenantId, {
            kind: "adgroup",
            campaign: found.campaign,
            adgroup,
          })
          const original = roasOf(found.campaign, adgroup)
          if (original == null) {
            planned.push(info(groupRef, adgroup.name, "configuration_missing"))
            continue
          }
          const next = adjust(original, mutation)
          if (next === null) {
            planned.push(
              info(
                groupRef,
                adgroup.name,
                "final_value_invalid",
                money(original),
              ),
            )
            continue
          }
          const campaign = found.campaign
          planned.push(
            pending(
              groupRef,
              adgroup.name,
              money(original),
              money(next),
              () => {
                adgroupRoas.set(adgroup.adgroup_id, next)
                // 报表里系列的目标 ROAS 列跟随其广告组。
                campaign.target_roas = next
              },
            ),
          )
        }
        break
      }
      case "name": {
        if (found.kind !== "campaign") {
          planned.push(info(ref, name, "management_capability_unverified"))
          break
        }
        const campaign = found.campaign
        const next = renamed(campaign.name, mutation, campaign.campaign_id)
        planned.push(
          pending(ref, name, campaign.name, next, () => {
            campaign.name = next
          }),
        )
        break
      }
    }
  }
  return planned
}

function countsOf(selection: StoredSelection, items: ManagementItemPublic[]) {
  const result = (r: ManagementItemPublic["execution_result"]) =>
    items.filter((item) => item.execution_result === r).length
  const counts: ManagementCounts = {
    selected: selection.refs.length + selection.materialUses.length,
    targets: items.filter((item) => item.reason !== "linked_impact").length,
    linked: items.filter((item) => item.reason === "linked_impact").length,
    unsupported: result("UNSUPPORTED") + result("CONFLICT"),
    pending: result("PENDING"),
    accepted: result("ACCEPTED"),
    rejected: result("REJECTED"),
    unknown: result("UNKNOWN"),
    not_sent: result("NOT_SENT"),
  }
  return counts
}

interface StoredPreview {
  public: ManagementPreviewPublic
  planned: PlannedItem[]
  mutation: MutationSpec
  selection: StoredSelection
  restoreOf?: string
}
const previews = new Map<string, StoredPreview>()

function storePreview(
  tenantId: string,
  bcId: string,
  selection: StoredSelection,
  mutation: MutationSpec,
  planned: PlannedItem[],
  restoreOf?: string,
) {
  const previewId = demoUuid()
  const items = planned.map((entry) => entry.item)
  const preview: ManagementPreviewPublic = {
    preview_id: previewId,
    digest: previewId.replace(/-/g, "").padEnd(64, "0"),
    created_at: isoAt(now()),
    expires_at: isoAt(now() + 5 * 60 * 1000),
    bc_id: bcId,
    route: route(tenantId, bcId),
    items,
    counts: countsOf(selection, items),
  }
  previews.set(previewId, {
    public: preview,
    planned,
    mutation,
    selection,
    restoreOf,
  })
  return preview
}

handle("ad_management-prepare_preview", ({ path, body }) => {
  const selection = selections.get(body.selection_id)
  if (!selection)
    return demoError(409, "selection_expired", "选择已过期，请重新勾选")
  return storePreview(
    path.tenant_id,
    body.bc_id,
    selection,
    body.mutation,
    plan(path.tenant_id, selection, body.mutation),
  )
})

// ========== 任务 ==========

interface StoredTask {
  task_id: string
  tenant_id: string
  direct?: boolean
  preparationProblems?: ManagementTaskPublic["preparation_problems"]
  bc_id: string
  created_at: string
  started: number
  actor_name: string
  mutation: MutationSpec
  source: ManagementTaskPublic["source"]
  planned: PlannedItem[]
  selection: StoredSelection
  /** 预置的历史任务直接给出终态。 */
  fixed?: ManagementTaskPublic["status"]
  cancelled?: boolean
  applied?: boolean
  /** 部分失败的演示：这些对象被平台拒绝。 */
  rejectIndexes?: number[]
}
const tasks: StoredTask[] = []

const TASK_STAGES = [
  ["QUEUED", 1000],
  ["RUNNING", 2000],
  ["DONE", 0],
] as const

const DIRECT_TASK_STAGES = [
  ["PREPARING", 1000],
  ["READY", 500],
  ["RUNNING", 2000],
  ["DONE", 0],
] as const

function taskView(task: StoredTask): ManagementTaskPublic {
  const stage = stageAt(
    task.started,
    task.direct ? DIRECT_TASK_STAGES : TASK_STAGES,
  )
  const done = !task.cancelled && (task.fixed !== undefined || stage.done)
  // 执行完成的那一刻把修改写回演示数据（只写一次）。
  if (done && !task.applied && task.fixed === undefined) {
    task.applied = true
    task.planned.forEach((entry, index) => {
      if (!task.rejectIndexes?.includes(index)) entry.apply?.()
    })
  }
  const items = task.planned.map((entry, index): ManagementItemPublic => {
    if (entry.item.execution_result !== "PENDING") return entry.item
    if (task.cancelled) return { ...entry.item, execution_result: "CANCELLED" }
    if (!done) return entry.item
    const rejected = task.rejectIndexes?.includes(index)
    return {
      ...entry.item,
      execution_result: rejected ? "REJECTED" : "ACCEPTED",
      reason: rejected ? "平台拒绝：账户余额不足" : null,
      delivery_status: rejected ? null : "DELIVERED",
      // 演示数据里修改即时生效，回读核对一致。
      observation_state: rejected ? null : "ACCEPTED",
      attempts: [
        {
          attempt: 1,
          request_at: isoAt(task.started + 1200),
          outcome: rejected ? "REJECTED" : "ACCEPTED",
          request_id: `20${task.task_id.slice(-10)}${index}`,
          retryable: rejected,
        },
      ],
    }
  })
  const counts = countsOf(task.selection, items)
  if (task.direct) {
    counts.prepared = stage.stage === "PREPARING" ? 0 : counts.selected
    counts.skipped = items.filter(
      (item) =>
        item.execution_result === "NO_CHANGE" ||
        item.execution_result === "UNSUPPORTED",
    ).length
  }
  const status: ManagementTaskPublic["status"] = task.cancelled
    ? "CANCELLED"
    : (task.fixed ??
      (!done
        ? (stage.stage as "PREPARING" | "READY" | "QUEUED" | "RUNNING")
        : (counts.rejected ?? 0) > 0
          ? (counts.accepted ?? 0) > 0
            ? "PARTIAL"
            : "FAILED"
          : "SUCCEEDED"))
  const targetKinds: Record<string, number> = {}
  for (const item of items)
    if (item.reason !== "linked_impact")
      targetKinds[item.material_use ? "material" : item.ref.kind] =
        (targetKinds[item.material_use ? "material" : item.ref.kind] ?? 0) + 1
  return {
    task_id: task.task_id,
    bc_id: task.bc_id,
    created_at: task.created_at,
    actor_name: task.actor_name,
    mutation: {
      field: task.mutation.field,
      mode: task.mutation.mode,
      value: task.mutation.value == null ? null : String(task.mutation.value),
      rename_kind: task.mutation.rename?.kind ?? null,
    },
    source: task.source,
    target_kinds: targetKinds,
    status,
    counts:
      task.direct && stage.stage === "PREPARING" && !task.cancelled
        ? { selected: counts.selected, prepared: 0, targets: 0, pending: 0 }
        : counts,
    items: task.direct && stage.stage === "PREPARING" ? [] : items,
    items_total: task.direct && stage.stage === "PREPARING" ? 0 : items.length,
    items_offset: 0,
    next_items_offset: null,
    preparation_problems: task.preparationProblems ?? [],
  }
}

// 执行时长结束后主动结算一次：即使没有页面在轮询任务，报表也能读到修改后的值。
function settleLater(task: StoredTask) {
  const total = (task.direct ? DIRECT_TASK_STAGES : TASK_STAGES).reduce(
    (sum, [, ms]) => sum + ms,
    0,
  )
  setTimeout(() => taskView(task), total + 50)
}

handle("ad_management-submit_task", ({ path, body }) => {
  const preview = previews.get(body.preview_id)
  if (!preview || preview.public.digest !== body.preview_digest)
    return demoError(409, "preview_expired", "预览已过期，请重新预览")
  // 同一幂等键重复提交返回同一个任务。
  const existing = tasks.find((task) => task.task_id === body.idempotency_key)
  if (existing) return new DemoReply(202, taskView(existing))
  const task: StoredTask = {
    tenant_id: path.tenant_id,
    task_id: body.idempotency_key.match(/^[0-9a-f-]{36}$/)
      ? body.idempotency_key
      : demoUuid(),
    bc_id: preview.public.bc_id,
    created_at: isoAt(now()),
    started: now(),
    actor_name: DEMO_USER.full_name,
    mutation: preview.mutation,
    source: { kind: "manual" },
    planned: preview.planned,
    selection: preview.selection,
  }
  tasks.unshift(task)
  previews.delete(body.preview_id)
  settleLater(task)
  void path
  return new DemoReply(202, taskView(task))
})

// 直接提交保留冻结输入与幂等映射；同键重试先返回已有任务，不依赖选择继续有效。
const directRequests = new Map<string, { digest: string; task: StoredTask }>()
function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical)
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonical(item)]),
    )
  return value
}

handle("ad_management-submit_direct_task", ({ path, body }) => {
  const key = `${path.tenant_id}:${body.idempotency_key}`
  const digest = JSON.stringify(
    canonical({ ...body, row_problems: body.row_problems ?? [] }),
  )
  const existing = directRequests.get(key)
  if (existing)
    return existing.digest === digest
      ? new DemoReply(202, taskView(existing.task))
      : demoError(409, "idempotency_conflict", "同一提交标识不能用于不同操作")
  const selection = selections.get(body.selection_id)
  if (!selection || selection.expiresAt <= now())
    return demoError(409, "selection_expired", "选择已过期，请重新勾选")
  if (selection.tenantId !== path.tenant_id || selection.bcId !== body.bc_id)
    return demoError(409, "selection_scope_mismatch", "选择不属于当前租户或 BC")
  const frozenSelection = structuredClone(selection)
  const mutation = structuredClone(body.mutation)
  const task: StoredTask = {
    tenant_id: path.tenant_id,
    task_id: demoUuid(),
    bc_id: body.bc_id,
    created_at: isoAt(now()),
    started: now(),
    actor_name: DEMO_USER.full_name,
    mutation,
    source: { kind: "manual" },
    direct: true,
    planned: plan(path.tenant_id, frozenSelection, mutation),
    selection: frozenSelection,
    preparationProblems: (body.row_problems ?? []).map((problem) => ({
      ...problem,
    })),
  }
  directRequests.set(key, { digest, task })
  tasks.unshift(task)
  settleLater(task)
  return new DemoReply(202, taskView(task))
})

const ACTION_FILTER: Record<string, (task: StoredTask) => boolean> = {
  enable: (t) => t.mutation.field === "status" && t.mutation.value === "ENABLE",
  disable: (t) =>
    t.mutation.field === "status" && t.mutation.value === "DISABLE",
  budget: (t) => t.mutation.field === "budget",
  roas: (t) => t.mutation.field === "roas",
  rename: (t) => t.mutation.field === "name",
}

handle("ad_management-list_tasks", ({ path, query }) => {
  const filtered = tasks.filter(
    (task) =>
      task.tenant_id === path.tenant_id &&
      task.bc_id === query.bc_id &&
      (!query.action || ACTION_FILTER[query.action]?.(task)) &&
      (!query.source || task.source?.kind === query.source),
  )
  return paginate(filtered.map(taskView), query, (task) => task.task_id)
})

const taskById = (id: string, tenantId: string) =>
  tasks.find((task) => task.task_id === id && task.tenant_id === tenantId)

handle("ad_management-get_task", ({ path, query }) => {
  const task = taskById(path.task_id, path.tenant_id)
  if (!task || task.tenant_id !== path.tenant_id)
    return demoError(404, "task_not_found", "任务不存在")
  const view = taskView(task)
  const offset = Math.max(0, query.offset ?? 0)
  const limit = Math.max(1, Math.min(500, query.limit ?? 100))
  const items = view.items ?? []
  return {
    ...view,
    items: items.slice(offset, offset + limit),
    items_total: items.length,
    items_offset: offset,
    next_items_offset: offset + limit < items.length ? offset + limit : null,
  }
})

handle("ad_management-cancel_task", ({ path }) => {
  const task = taskById(path.task_id, path.tenant_id)
  if (!task) return demoError(404, "task_not_found", "任务不存在")
  const view = taskView(task)
  if (["PREPARING", "READY", "QUEUED"].includes(view.status))
    task.cancelled = true
  return taskView(task)
})

// 失败项重试：被拒绝的对象重新执行，几秒后成功。
handle("ad_management-retry_task", ({ path }) => {
  const task = taskById(path.task_id, path.tenant_id)
  if (!task) return demoError(404, "task_not_found", "任务不存在")
  const rejected = task.rejectIndexes ?? []
  task.rejectIndexes = []
  task.fixed = undefined
  task.started = now()
  task.applied = false
  // 已成功的对象不再重复写回，只补做被拒绝的对象。
  task.planned = task.planned.map((entry, index) =>
    rejected.includes(index) ? entry : { ...entry, apply: undefined },
  )
  settleLater(task)
  return new DemoReply(202, taskView(task))
})

// 核查结果未知的对象：演示数据里核查后都确认已生效。
handle("ad_management-reconcile_task", ({ path }) =>
  taskById(path.task_id, path.tenant_id)
    ? taskView(taskById(path.task_id, path.tenant_id) as StoredTask)
    : demoError(404, "task_not_found", "任务不存在"),
)

handle("ad_management-retry_targeted_refresh", ({ path }) => {
  const task = taskById(path.task_id, path.tenant_id)
  return task
    ? new DemoReply(202, taskView(task))
    : demoError(404, "task_not_found", "任务不存在")
})

// 一键恢复：把任务里已成功的对象改回原值。
handle("ad_management-prepare_restore", ({ path }) => {
  const task = taskById(path.task_id, path.tenant_id)
  if (!task) return demoError(404, "task_not_found", "任务不存在")
  const view = taskView(task)
  const restorable = view.items?.filter(
    (item) =>
      item.execution_result === "ACCEPTED" &&
      item.original_value != null &&
      !item.material_use,
  )
  const selection: StoredSelection = {
    refs: (restorable ?? []).map((item) => item.ref),
    materialUses: [],
  }
  const planned = (restorable ?? []).flatMap((item) => {
    const mutation: MutationSpec = {
      field: task.mutation.field,
      mode: "set",
      value:
        task.mutation.field === "name" || task.mutation.field === "status"
          ? (item.original_value as string)
          : Number(item.original_value),
      rename:
        task.mutation.field === "name"
          ? {
              kind: "explicit",
              names: { [item.ref.remote_id]: item.original_value as string },
            }
          : null,
    }
    return plan(
      path.tenant_id,
      { refs: [item.ref], materialUses: [] },
      mutation,
    )
  })
  return storePreview(
    path.tenant_id,
    task.bc_id,
    selection,
    { ...task.mutation, mode: "set" },
    planned,
    task.task_id,
  )
})

// ========== 预置历史任务 ==========

function seedTask(input: {
  daysAgo: number
  hour: number
  mutation: MutationSpec
  campaigns: DemoLaunchedCampaign[]
  status: ManagementTaskPublic["status"]
  source?: ManagementTaskPublic["source"]
  actor?: string
  rejectLast?: boolean
}) {
  const selection: StoredSelection = {
    refs: input.campaigns.map((campaign) =>
      refOf(MAIN_TENANT_ID, { kind: "campaign", campaign }),
    ),
    materialUses: [],
  }
  const planned = plan(MAIN_TENANT_ID, selection, input.mutation).map(
    (entry) => ({ ...entry, apply: undefined }),
  )
  const started = Date.parse(isoDaysAgo(input.daysAgo, input.hour))
  const task: StoredTask = {
    tenant_id: MAIN_TENANT_ID,
    task_id: demoUuid(nextId()),
    bc_id: MAIN_BC_ID,
    created_at: isoAt(started),
    started,
    actor_name: input.actor ?? "周航",
    mutation: input.mutation,
    source: input.source ?? { kind: "manual" },
    planned,
    selection,
    fixed: input.status === "PARTIAL" ? undefined : input.status,
    cancelled: input.status === "CANCELLED",
    applied: true,
    rejectIndexes: input.rejectLast
      ? [planned.map((e) => e.item.execution_result).lastIndexOf("PENDING")]
      : undefined,
  }
  if (input.status === "SUCCEEDED") task.fixed = undefined
  tasks.push(task)
}

const live = demoWorld.launched.filter((c) => c.bc_id === MAIN_BC_ID)
seedTask({
  daysAgo: 0,
  hour: 9,
  mutation: { field: "budget", mode: "increase_percent", value: 20 },
  campaigns: live.slice(0, 3),
  status: "SUCCEEDED",
  source: { kind: "rule", rule_id: null, rule_name: "单组消耗达标加预算" },
  actor: "自动规则",
})
seedTask({
  daysAgo: 1,
  hour: 15,
  mutation: { field: "status", mode: "set", value: "DISABLE" },
  campaigns: live.slice(3, 6),
  status: "SUCCEEDED",
})
seedTask({
  daysAgo: 2,
  hour: 11,
  mutation: { field: "roas", mode: "set", value: 1.15 },
  // 只有目标 ROAS 出价的系列可以改 ROAS。
  campaigns: live.filter((c) => c.target_roas != null).slice(2, 4),
  status: "PARTIAL",
  actor: "陈曦",
  rejectLast: true,
})
seedTask({
  daysAgo: 3,
  hour: 20,
  mutation: { field: "budget", mode: "set", value: 150 },
  campaigns: live.slice(8, 10),
  status: "CANCELLED",
  actor: "林悦",
})
