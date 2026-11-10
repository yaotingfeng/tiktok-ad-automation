import type { CopyDraftPublic, CopyRow } from "@/features/campaign-copies/api"
import { configurationError } from "@/features/campaign-copies/api"
import { normalizedTargeting } from "@/features/targeting/TargetingForm"
import { demoDrafts, demoPreviews } from "../fixtures/builds-data"
import {
  DEMO_ADVERTISERS,
  DEMO_DRAMAS,
  DEMO_IDENTITIES,
  DEMO_MATERIALS,
  DEMO_STRATEGIES,
  demoWorld,
} from "../fixtures/world"
import { demoUuid, isoNow, now } from "../runtime/clock"
import {
  DemoReply,
  demoError,
  handle,
  resolveDemoRoute,
} from "../runtime/registry"
import { getDemoFrozenSelection } from "./ads-management"

// 体验版走共享客户端的本地适配器，不访问网络或创建真实广告。
const drafts = new Map<
  string,
  { tenantId: string; draft: CopyDraftPublic; readyAt: number }
>()
const requests = new Map<string, { fingerprint: string; draftId: string }>()
function replay(tenantId: string, requestId: string, payload: unknown) {
  const saved = requests.get(`${tenantId}:${requestId}`)
  if (!saved) return null
  if (saved.fingerprint !== JSON.stringify(payload))
    return demoError(409, "copy_request_conflict", "请求标识已用于其他配置")
  return structuredClone(drafts.get(saved.draftId)!.draft)
}
function record(
  tenantId: string,
  requestId: string,
  payload: unknown,
  draft: CopyDraftPublic,
) {
  requests.set(`${tenantId}:${requestId}`, {
    fingerprint: JSON.stringify(payload),
    draftId: draft.draft_id,
  })
  return structuredClone(draft)
}
function read(tenantId: string, draftId: string) {
  const stored = drafts.get(draftId)
  return stored?.tenantId === tenantId ? stored : null
}
const notFound = () => demoError(404, "copy_draft_not_found", "复制草稿不存在")
function revisionError(draft: CopyDraftPublic, revision: number) {
  return draft.revision !== revision
    ? demoError(409, "copy_revision_conflict", "草稿修订已更新，请重新载入")
    : null
}
handle("campaign_copies-create", ({ path, body }) => {
  const payload = { kind: "create", body }
  const previous = replay(path.tenant_id, body.request_id, payload)
  if (previous) return previous
  const selection = getDemoFrozenSelection(
    body.selection_id,
    path.tenant_id,
    body.bc_id,
  )
  if (
    !selection?.refs.length ||
    selection.refs.some((ref) => ref.kind !== "campaign")
  )
    return demoError(409, "copy_selection_invalid", "请选择当前 BC 的来源系列")
  const campaigns = selection.refs.map((ref) =>
    demoWorld.launched.find(
      (campaign) =>
        campaign.bc_id === body.bc_id &&
        campaign.advertiser_id === ref.advertiser_id &&
        campaign.campaign_id === ref.remote_id,
    ),
  )
  if (campaigns.some((campaign) => !campaign))
    return demoError(409, "copy_source_missing", "来源系列已不可用")
  const rows: CopyRow[] = campaigns.map((campaign) => {
    const source = campaign!
    const ids = new Set(
      source.adgroups.flatMap((group) => group.ads.map((ad) => ad.material_id)),
    )
    const materials = DEMO_MATERIALS.filter((material) =>
      ids.has(material.id),
    ).map((material) => ({
      material_id: material.id,
      file_name: material.file_name,
      media_type: "VIDEO",
    }))
    return {
      row_id: demoUuid(),
      source: {
        advertiser_id: source.advertiser_id,
        campaign_id: source.campaign_id,
        name: source.name,
        currency: DEMO_ADVERTISERS.find(
          (account) => account.advertiser_id === source.advertiser_id,
        )!.currency,
        budget: String(source.budget),
        target_roas:
          source.target_roas === null ? null : String(source.target_roas),
        budget_strategy: "SERIES",
        bid_strategy:
          source.target_roas === null ? "HIGHEST_VALUE" : "TARGET_ROAS",
        materials,
        targeting: normalizedTargeting(),
        targeting_mixed: false,
        group_settings: source.adgroups.map((group) => ({
          name: group.name,
          budget: null,
          target_roas:
            source.target_roas === null ? null : String(source.target_roas),
          targeting: normalizedTargeting(),
        })),
        blockers: materials.length ? [] : ["source_material_mapping_missing"],
      },
      configuration: {
        target_advertiser_ids: [source.advertiser_id],
        copies: 1,
        budget: null,
        target_roas: null,
        targeting: null,
        delivery_config: {
          schedule_mode: "IMMEDIATE",
          schedule_start_time: null,
          schedule_end_time: null,
        },
        material_ids: materials.map((material) => material.material_id),
      },
    }
  })
  const draft: CopyDraftPublic = {
    draft_id: demoUuid(),
    bc_id: body.bc_id,
    revision: 1,
    status: "DRAFT",
    rows,
    preview_digest: null,
    children: [],
    submission_ids: [],
    error_code: null,
  }
  drafts.set(draft.draft_id, { tenantId: path.tenant_id, draft, readyAt: 0 })
  return new DemoReply(
    201,
    record(path.tenant_id, body.request_id, payload, draft),
  )
})
handle("campaign_copies-get", ({ path, query }) => {
  const stored = read(path.tenant_id, path.draft_id)
  if (!stored || stored.draft.bc_id !== query.bc_id) return notFound()
  if (stored.draft.status === "PREPARING" && now() >= stored.readyAt)
    stored.draft.status = "READY"
  return structuredClone(stored.draft)
})
handle("campaign_copies-patch", ({ path, body }) => {
  const payload = { kind: "patch", draftId: path.draft_id, body }
  const previous = replay(path.tenant_id, body.request_id, payload)
  if (previous) return previous
  const stored = read(path.tenant_id, path.draft_id)
  if (!stored) return notFound()
  const invalidRevision = revisionError(stored.draft, body.expected_revision)
  if (invalidRevision) return invalidRevision
  if (["PREPARING", "SUBMITTED"].includes(stored.draft.status))
    return demoError(409, "copy_draft_locked", "当前草稿不可修改")
  const rows = stored.draft.rows.map((row) => {
    const patch = body.rows.find((item) => item.row_id === row.row_id)
    return patch
      ? {
          ...row,
          configuration: structuredClone(
            patch.configuration,
          ) as CopyRow["configuration"],
        }
      : row
  })
  const invalid = configurationError(rows)
  if (invalid) return demoError(422, "copy_configuration_invalid", invalid)
  Object.assign(stored.draft, {
    rows,
    revision: stored.draft.revision + 1,
    status: "DRAFT",
    children: [],
    preview_digest: null,
    submission_ids: [],
    error_code: null,
  })
  return record(path.tenant_id, body.request_id, payload, stored.draft)
})
handle("campaign_copies-preview", ({ path, body }) => {
  const payload = { kind: "preview", draftId: path.draft_id, body }
  const previous = replay(path.tenant_id, body.request_id, payload)
  if (previous) return previous
  const stored = read(path.tenant_id, path.draft_id)
  if (!stored) return notFound()
  const { draft } = stored
  const invalidRevision = revisionError(draft, body.expected_revision)
  if (invalidRevision) return invalidRevision
  const invalid = configurationError(draft.rows)
  if (invalid) return demoError(422, "copy_configuration_invalid", invalid)
  if (draft.status === "SUBMITTED")
    return demoError(409, "copy_draft_locked", "复制草稿已提交")
  // 每份使用独立子预览，现有预览页和任务页可回读对应演示数据。
  draft.children = []
  for (const row of draft.rows) {
    const campaign = demoWorld.launched.find(
      (item) => item.campaign_id === row.source.campaign_id,
    )!
    const drama = DEMO_DRAMAS.find((item) => item.id === campaign.drama_id)!
    const strategy = DEMO_STRATEGIES.find(
      (item) => item.id === campaign.strategy_id,
    )!
    for (const advertiserId of row.configuration.target_advertiser_ids) {
      const account = DEMO_ADVERTISERS.find(
        (item) =>
          item.advertiser_id === advertiserId &&
          item.bc_id === draft.bc_id &&
          item.purpose === "delivery",
      )
      if (!account)
        return demoError(
          409,
          "copy_target_unavailable",
          "目标账户不可用于当前 BC 搭建",
        )
      for (let ordinal = 1; ordinal <= row.configuration.copies; ordinal++) {
        const draftId = demoUuid(),
          previewId = demoUuid()
        demoDrafts.push({
          draft_id: draftId,
          tenant_id: path.tenant_id,
          bc_id: draft.bc_id,
          revision: 1,
          status: "READY",
          preparation_phase: "done",
          strategy_version_id: strategy.version_id,
          provider_connection_id: drama.provider_id,
          execution_connection_id: demoUuid(301),
          application_id: "app-demo-001",
          link_config: {},
          delivery_config: row.configuration.delivery_config,
          targeting:
            row.configuration.targeting ??
            row.source.targeting ??
            normalizedTargeting(),
          targeting_override: row.configuration.targeting,
          drama_lines: [drama.title],
          account_lines: [advertiserId],
          drama_count: 1,
          account_count: 1,
          input_counts: { drama: { ready: 1 }, account: { ready: 1 } },
          task_id: null,
          provider_task_id: null,
          error_code: null,
          created_at: isoNow(),
          updated_at: isoNow(),
          selected_mini: { minis_id: "mini-demo-001", name: "DramaHub" },
          selected_identity: DEMO_IDENTITIES[0],
          prepare_started_at: null,
        })
        demoPreviews.push({
          preview_id: previewId,
          draft_id: draftId,
          draft_revision: 1,
          bc_id: draft.bc_id,
          status: "FROZEN",
          currency: account.currency,
          campaign_count: 1,
          adgroup_count: campaign.adgroups.length,
          ad_count: campaign.adgroups.reduce(
            (count, group) => count + group.ads.length,
            0,
          ),
          blocked_count: 0,
          preparing_count: 0,
          input_issue_count: 0,
          total_unit_count: 1,
          generation_progress: {
            phase: "complete",
            completed_units: 1,
            total_units: 1,
            updated_at: isoNow(),
          },
          daily_budget_sum:
            row.configuration.budget ?? row.source.budget ?? "0",
          daily_budget_label: "系列预算合计",
          budget_strategy: row.source.budget_strategy,
          bid_strategy: row.source.bid_strategy,
          group_generation_mode: "FIXED",
          ad_generation_mode: "FIXED",
          creative_count: 1,
          unique_material_count: row.configuration.material_ids.length,
          material_allocation_count: row.configuration.material_ids.length,
          structure_summary: `演示复制 · ${row.source.name} · 第 ${ordinal} 份`,
          content_digest: `demo-copy-${previewId}`,
          error_code: null,
          created_at: isoNow(),
          submission_id: null,
          execution_route: {
            connection_id: demoUuid(301),
            connection_name: "API 搭建连接",
            channel: "OFFICIAL_API",
            bc_id: draft.bc_id,
          },
          skipped_material_count: 0,
          build_started_at: null,
        })
        draft.children.push({
          row_id: row.row_id,
          advertiser_id: advertiserId,
          ordinal,
          preview_id: previewId,
          group_settings: row.source.group_settings?.map((group) => ({
            ...group,
            budget:
              row.source.budget_strategy === "ADGROUP"
                ? (row.configuration.budget ?? group.budget)
                : group.budget,
            target_roas: row.configuration.target_roas ?? group.target_roas,
            targeting: row.configuration.targeting ?? group.targeting,
          })),
          submission_id: null,
          status: "READY",
          error_code: null,
        })
      }
    }
  }
  draft.status = "PREPARING"
  draft.preview_digest = draft.draft_id
    .replace(/-/g, "")
    .padEnd(64, String(draft.revision % 10))
  stored.readyAt = now() + 1000
  return new DemoReply(
    202,
    record(path.tenant_id, body.request_id, payload, draft),
  )
})
handle("campaign_copies-submit", async ({ path, body }) => {
  const payload = { kind: "submit", draftId: path.draft_id, body }
  const previous = replay(path.tenant_id, body.request_id, payload)
  if (previous) return previous
  const stored = read(path.tenant_id, path.draft_id)
  if (!stored) return notFound()
  const { draft } = stored
  const invalidRevision = revisionError(draft, body.expected_revision)
  if (invalidRevision) return invalidRevision
  if (draft.status !== "READY" || draft.preview_digest !== body.preview_digest)
    return demoError(409, "copy_preview_not_ready", "请核对当前修订的复制预览")
  for (const child of draft.children) {
    const route = resolveDemoRoute(
      "POST",
      `/api/tenants/${path.tenant_id}/build-previews/${child.preview_id}/submit`,
    )
    if (!route?.handler)
      return demoError(
        500,
        "demo_copy_submit_missing",
        "演示搭建提交接口未注册",
      )
    const result = await route.handler({
      operationId: route.id,
      method: "POST",
      path: route.params,
      query: {},
      body: { request_id: demoUuid() },
      search: new URLSearchParams(),
      headers: {},
    })
    if (!(result instanceof DemoReply) || result.status !== 202) return result
    child.submission_id = (
      result.body as { submission_id: string }
    ).submission_id
    child.status = "SUBMITTED"
  }
  draft.submission_ids = draft.children.flatMap((child) =>
    child.submission_id ? [child.submission_id] : [],
  )
  draft.status = "SUBMITTED"
  return new DemoReply(
    202,
    record(path.tenant_id, body.request_id, payload, draft),
  )
})
