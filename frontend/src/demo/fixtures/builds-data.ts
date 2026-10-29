import { IMAGE_DEMO_STRUCTURE } from "./image-structure"
// 批量搭建领域的私有数据：草稿、预览、提交、执行步骤。
import type {
  AudienceTargeting,
  DeliveryConfig,
  StructureOverride,
} from "../../client"
import { demoUuid, isoDaysAgo } from "../runtime/clock"
import {
  DEMO_DRAMAS,
  DEMO_IDENTITIES,
  DEMO_PROVIDERS,
  DEMO_STRATEGIES,
  deliveryAdvertisers,
  MAIN_BC_ID,
  MAIN_TENANT_ID,
} from "./world"

// 草稿状态
export type DraftStatus = "DRAFT" | "PREPARING" | "READY"
export type PreviewStatus = "BUILDING" | "FROZEN" | "OBSOLETE"
export type UnitReadiness = "READY" | "PREPARING" | "BLOCKED"

// 草稿数据结构
export interface DemoDraft {
  media_kind?: "VIDEO" | "CAROUSEL"
  structure_override?: StructureOverride
  draft_id: string
  tenant_id: string
  bc_id: string
  revision: number
  status: DraftStatus
  preparation_phase: string
  strategy_version_id: string
  provider_connection_id: string
  execution_connection_id: string | null
  application_id: string
  link_config: Record<string, unknown>
  delivery_config: DeliveryConfig
  targeting: AudienceTargeting
  targeting_override: AudienceTargeting | null
  drama_lines: string[]
  account_lines: string[]
  drama_count: number
  account_count: number
  input_counts: { drama: { ready: number }; account: { ready: number } }
  task_id: string | null
  provider_task_id: string | null
  error_code: string | null
  created_at: string
  updated_at: string
  // 运行时：选中的 mini 和 identity
  selected_mini: { minis_id: string; name: string } | null
  selected_identity: {
    identity_id: string
    identity_type: string
    display_name: string
  } | null
  // 运行时：准备开始时间（用于时间推进）
  prepare_started_at: number | null
}

// 预览数据结构
export interface DemoPreview {
  preview_id: string
  draft_id: string
  draft_revision: number
  bc_id: string
  status: PreviewStatus
  currency: string
  campaign_count: number
  adgroup_count: number
  ad_count: number
  blocked_count: number
  preparing_count: number
  input_issue_count: number
  total_unit_count: number
  generation_progress: {
    phase: string
    completed_units: number
    total_units: number | null
    updated_at: string
  }
  daily_budget_sum: string
  daily_budget_label: string
  budget_strategy: string
  bid_strategy: string
  group_generation_mode: string
  ad_generation_mode: string
  creative_count: number
  unique_material_count: number
  material_allocation_count: number
  structure_summary: string
  content_digest: string
  error_code: string | null
  created_at: string
  submission_id: string | null
  execution_route: {
    connection_id: string
    connection_name: string
    channel: string
    bc_id: string
  } | null
  skipped_material_count: number
  // 运行时：生成开始时间
  build_started_at: number | null
}

// 内存状态：所有草稿
export const demoDrafts: DemoDraft[] = []

// 内存状态：所有预览
export const demoPreviews: DemoPreview[] = []

// 请求幂等键映射
export const demoMutationRequests = new Map<
  string,
  { draft_id: string; revision: number }
>()
export const demoPreviewRequests = new Map<string, { preview_id: string }>()

// 初始化预置草稿（历史提交在 handlers/builds-submissions.ts）
export function seedHistorySubmissions() {
  const delivery = deliveryAdvertisers(MAIN_BC_ID)
  // 预置两个未提交的草稿
  const draft1: DemoDraft = {
    draft_id: demoUuid(721),
    tenant_id: MAIN_TENANT_ID,
    bc_id: MAIN_BC_ID,
    revision: 1,
    status: "READY",
    preparation_phase: "done",
    strategy_version_id: DEMO_STRATEGIES[0].version_id,
    provider_connection_id: DEMO_PROVIDERS[0].id,
    execution_connection_id: demoUuid(301),
    application_id: "app-demo-001",
    link_config: {},
    delivery_config: {
      schedule_mode: "IMMEDIATE",
      schedule_start_time: null,
      schedule_end_time: null,
    },
    targeting: {
      region_mode: "ALL_AVAILABLE",
      region_codes: [],
      languages: [],
      age_groups: [],
      gender: "GENDER_UNLIMITED",
    },
    targeting_override: null,
    drama_lines: [DEMO_DRAMAS[0].title, DEMO_DRAMAS[1].title],
    account_lines: [delivery[0].advertiser_id, delivery[1].advertiser_id],
    drama_count: 2,
    account_count: 2,
    input_counts: { drama: { ready: 2 }, account: { ready: 2 } },
    task_id: null,
    provider_task_id: null,
    error_code: null,
    created_at: isoDaysAgo(2, 14, 30),
    updated_at: isoDaysAgo(2, 15, 10),
    selected_mini: { minis_id: "mini-demo-001", name: "DramaHub" },
    selected_identity: {
      identity_id: DEMO_IDENTITIES[0].identity_id,
      identity_type: DEMO_IDENTITIES[0].identity_type,
      display_name: DEMO_IDENTITIES[0].display_name,
    },
    prepare_started_at: null,
  }

  const draft2: DemoDraft = {
    draft_id: demoUuid(722),
    tenant_id: MAIN_TENANT_ID,
    bc_id: MAIN_BC_ID,
    revision: 3,
    status: "DRAFT",
    preparation_phase: "not_started",
    strategy_version_id: DEMO_STRATEGIES[1].version_id,
    provider_connection_id: DEMO_PROVIDERS[1].id,
    execution_connection_id: null,
    application_id: "app-demo-002",
    link_config: {},
    delivery_config: {
      schedule_mode: "IMMEDIATE",
      schedule_start_time: null,
      schedule_end_time: null,
    },
    targeting: {
      region_mode: "ALL_AVAILABLE",
      region_codes: [],
      languages: [],
      age_groups: [],
      gender: "GENDER_UNLIMITED",
    },
    targeting_override: null,
    drama_lines: [DEMO_DRAMAS[2].title],
    account_lines: [delivery[0].advertiser_id],
    drama_count: 1,
    account_count: 1,
    input_counts: { drama: { ready: 0 }, account: { ready: 1 } },
    task_id: null,
    provider_task_id: null,
    error_code: null,
    created_at: isoDaysAgo(5, 10, 0),
    updated_at: isoDaysAgo(1, 16, 45),
    selected_mini: null,
    selected_identity: null,
    prepare_started_at: null,
  }

  demoDrafts.push(draft1, draft2, {
    ...draft1,
    draft_id: demoUuid(723),
    media_kind: "CAROUSEL",
    revision: 1,
    structure_override: { ...IMAGE_DEMO_STRUCTURE },
  })
}

// 初始化时调用
seedHistorySubmissions()
