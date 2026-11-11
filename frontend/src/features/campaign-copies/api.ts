import type { AudienceTargeting, DeliveryConfig } from "@/client"
import { client } from "@/client/client.gen"

export type CopyConfiguration = {
  target_advertiser_ids: string[]
  copies: number
  budget: string | null
  target_roas: string | null
  targeting: AudienceTargeting | null
  delivery_config: DeliveryConfig
  material_ids: string[]
}
export type CopyGroupSetting = {
  name: string
  budget: string | null
  target_roas: string | null
  targeting: AudienceTargeting | null
  targeting_summary?: string
}
export type CopyRow = {
  row_id: string
  source: {
    advertiser_id: string
    campaign_id: string
    name: string
    currency: string
    provider_name?: string
    application_name?: string
    minis_id?: string
    budget: string | null
    target_roas: string | null
    budget_strategy: "SERIES" | "ADGROUP"
    bid_strategy: "TARGET_ROAS" | "HIGHEST_VALUE"
    materials: { material_id: string; file_name: string; media_type: string }[]
    targeting: AudienceTargeting | null
    targeting_mixed?: boolean
    group_settings?: CopyGroupSetting[]
    blockers: string[]
  }
  configuration: CopyConfiguration
}
export type CopyDraftPublic = {
  draft_id: string
  bc_id: string
  revision: number
  status: "DRAFT" | "PREPARING" | "READY" | "SUBMITTED" | "FAILED"
  rows: CopyRow[]
  preview_digest: string | null
  children: {
    row_id: string
    advertiser_id: string
    ordinal: number
    preview_id: string | null
    submission_id: string | null
    status: string
    error_code: string | null
    group_settings?: CopyGroupSetting[]
  }[]
  submission_ids: string[]
  error_code: string | null
}
export type CopyRowsPatch = {
  row_id: string
  configuration: CopyConfiguration
}[]
export type CopyMutation =
  | {
      kind: "save"
      body: {
        request_id: string
        expected_revision: number
        rows: CopyRowsPatch
      }
    }
  | {
      kind: "preview" | "refresh-source"
      body: { request_id: string; expected_revision: number }
    }
  | {
      kind: "submit"
      body: {
        request_id: string
        expected_revision: number
        preview_digest: string
      }
    }
const security = [{ scheme: "bearer", type: "http" }] as const
const base = (tenantId: string) =>
  `/api/tenants/${encodeURIComponent(tenantId)}/campaign-copies`
const path = (tenantId: string, draftId: string) =>
  `${base(tenantId)}/${encodeURIComponent(draftId)}`
export const copyKey = (tenantId: string, bcId: string, draftId: string) =>
  ["tenant", tenantId, "campaign-copies", bcId, draftId] as const
export const pendingKey = (tenantId: string, bcId: string, draftId: string) =>
  `campaign-copy:${tenantId}:${bcId}:${draftId}:pending`
export const scopeKey = (tenantId: string, draftId: string) =>
  `campaign-copy:${tenantId}:${draftId}:bc`

export async function createCopyDraft(
  tenantId: string,
  body: { bc_id: string; selection_id: string; request_id: string },
) {
  const { data } = await client.post<{ 200: CopyDraftPublic }, unknown, true>({
    url: base(tenantId),
    body,
    security: [...security],
    responseType: "json",
    headers: { "Content-Type": "application/json" },
  })
  sessionStorage.setItem(scopeKey(tenantId, data.draft_id), data.bc_id)
  return data
}
export async function getCopyDraft(
  tenantId: string,
  bcId: string,
  draftId: string,
  signal: AbortSignal,
) {
  const { data } = await client.get<{ 200: CopyDraftPublic }, unknown, true>({
    url: path(tenantId, draftId),
    query: { bc_id: bcId },
    signal,
    security: [...security],
    responseType: "json",
  })
  sessionStorage.setItem(scopeKey(tenantId, draftId), data.bc_id)
  return data
}
export async function mutateCopyDraft(
  tenantId: string,
  draftId: string,
  mutation: CopyMutation,
) {
  const options = {
    url: `${path(tenantId, draftId)}${mutation.kind === "save" ? "" : `/${mutation.kind}`}`,
    body: mutation.body,
    security: [...security],
    responseType: "json" as const,
    headers: { "Content-Type": "application/json" },
  }
  const { data } =
    mutation.kind === "save"
      ? await client.patch<{ 200: CopyDraftPublic }, unknown, true>(options)
      : await client.post<{ 200: CopyDraftPublic }, unknown, true>(options)
  return data
}
export function readPending(key: string): CopyMutation | null {
  try {
    const value: CopyMutation | null = JSON.parse(
      sessionStorage.getItem(key) || "null",
    )
    return value &&
      ["save", "preview", "submit", "refresh-source"].includes(value.kind) &&
      typeof value.body?.request_id === "string"
      ? value
      : null
  } catch {
    return null
  }
}
export function configurationError(rows: CopyRow[]) {
  if (!rows.length || rows.length > 100) return "每批需要 1–100 条来源系列。"
  let expanded = 0
  for (const row of rows) {
    const config = row.configuration
    if (row.source.blockers.length)
      return `${row.source.name}：${row.source.blockers.join("；")}`
    if (
      !config.target_advertiser_ids.length ||
      config.target_advertiser_ids.length > 100
    )
      return "每条来源请选择 1–100 个目标账户。"
    if (
      !Number.isInteger(config.copies) ||
      config.copies < 1 ||
      config.copies > 20
    )
      return "每账户份数须为 1–20 的整数。"
    if (!config.material_ids.length) return "每条来源至少保留一条素材。"
    if (
      config.budget !== null &&
      (!Number.isFinite(Number(config.budget)) || Number(config.budget) <= 0)
    )
      return "预算须为大于 0 的数值。"
    if (
      config.target_roas !== null &&
      (!Number.isFinite(Number(config.target_roas)) ||
        Number(config.target_roas) <= 0)
    )
      return "目标 ROAS 须为大于 0 的数值。"
    expanded += config.target_advertiser_ids.length * config.copies
  }
  return expanded > 1000 ? "本批最多生成 1000 个新系列，请减少账户或份数。" : ""
}
