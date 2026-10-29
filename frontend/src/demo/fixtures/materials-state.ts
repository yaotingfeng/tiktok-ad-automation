import { imageCandidates } from "./image-composition-state"
// 素材领域运行时状态：ingest 会话、上传任务、素材制作状态
import {
  demoNumericId,
  demoUuid,
  isoAt,
  nextId,
  now,
  stageAt,
} from "../runtime/clock"
import {
  DEMO_DRAMAS,
  DEMO_MATERIALS,
  demoCover,
  materialAdvertisers,
} from "./world"

export interface IngestSessionState {
  session_id: string
  bc_id: string
  expected_count: number
  created_at: string
  files: IngestFileState[]
  sealed: boolean
}

export interface IngestFileState {
  material_id: string
  client_index: number
  file_name: string
  size: number
  mime_type: string
  last_modified_ms: number
  generation: number
  upload_id: string
  part_size: number
  part_count: number
  operation_revision: number
  received_bytes: number
  parts: IngestPartState[]
  completed: boolean
  /** 原文件接收完成的时间；之后平台上传按时间推进到“账户素材可用”。 */
  stored_at: number | null
  /** 已进入素材库（只入库一次）。 */
  published: boolean
  error_code: string | null
  source_advertiser_id: string | null
}

export interface IngestPartState {
  part_number: number
  byte_size: number
  etag: string
}

// 存储所有活跃的 ingest 会话
export const ingestSessions = new Map<string, IngestSessionState>()

// 存储所有异步请求的状态（chunk 注册、文件完成等）
export const ingestRequests = new Map<
  string,
  {
    request_id: string
    status: "processing" | "complete" | "error"
    started_at: number
    result?: unknown
  }
>()

// 素材制作状态（全局单例，按租户）
export const materialProductionState = new Map<
  string,
  {
    tenant_id: string
    enabled: boolean
    workspace_url: string
  }
>()

// 初始化素材制作状态
export function ensureMaterialProduction(tenantId: string) {
  if (!materialProductionState.has(tenantId)) {
    materialProductionState.set(tenantId, {
      tenant_id: tenantId,
      enabled: true,
      workspace_url: "https://manjumt.gzjunbo.net",
    })
  }
  return materialProductionState.get(tenantId)!
}

// 创建 ingest 会话
export function createIngestSession(
  bcId: string,
  expectedCount: number,
): IngestSessionState {
  const session: IngestSessionState = {
    session_id: demoUuid(),
    bc_id: bcId,
    expected_count: expectedCount,
    created_at: isoAt(now()),
    files: [],
    sealed: false,
  }
  ingestSessions.set(session.session_id, session)
  return session
}

// 添加文件到会话
export function addIngestFile(
  sessionId: string,
  clientIndex: number,
  fileName: string,
  size: number,
  mimeType: string,
  lastModifiedMs: number,
): IngestFileState {
  const session = ingestSessions.get(sessionId)
  if (!session) throw new Error("Session not found")

  const partSize = 16 * 1024 * 1024 // 16MB
  const partCount = Math.ceil(size / partSize)

  const file: IngestFileState = {
    material_id: demoUuid(),
    client_index: clientIndex,
    file_name: fileName,
    size: size,
    mime_type: mimeType,
    last_modified_ms: lastModifiedMs,
    generation: 1,
    upload_id: demoNumericId(),
    part_size: partSize,
    part_count: partCount,
    operation_revision: 1,
    received_bytes: 0,
    parts: [],
    completed: false,
    stored_at: null,
    published: false,
    error_code: null,
    source_advertiser_id: null,
  }

  session.files.push(file)
  return file
}

// 原文件接收完成：记录时间，后续阶段由 fileStage 按时间推进。
export function completeIngestFile(sessionId: string, materialId: string) {
  const session = ingestSessions.get(sessionId)
  if (!session) throw new Error("Session not found")
  const file = session.files.find((f) => f.material_id === materialId)
  if (!file) throw new Error("File not found")
  if (file.completed) return
  file.completed = true
  file.received_bytes = file.size
  file.stored_at = now()
}

// 原文件接收后的演示节奏：原件校验、上传 TikTok、核实，约 6 秒后账户素材可用。
const PLATFORM_STAGES = [
  ["pending", 1200],
  ["uploading", 2000],
  ["verifying", 1800],
  ["available", 0],
] as const
const STORAGE_STAGES = [
  ["stored", 1500],
  ["validating", 1500],
  ["verified", 4000],
  ["deleted", 0],
] as const

export function fileStage(file: IngestFileState, bcId: string) {
  if (!file.completed || file.stored_at === null)
    return {
      platform: "pending",
      storage: "receiving",
      available: false,
    }
  const platform = stageAt(file.stored_at, PLATFORM_STAGES)
  const storage = stageAt(file.stored_at, STORAGE_STAGES)
  if (platform.done && !file.published) publishMaterial(file, bcId)
  return {
    platform: platform.stage as string,
    storage: storage.stage as string,
    available: platform.done,
  }
}

// 入库：按文件名里的完整剧名匹配剧（真实规则），之后搭建页即可按剧名匹配到这条素材。
function publishMaterial(file: IngestFileState, bcId: string) {
  file.published = true
  const lower = file.file_name.toLowerCase()
  const drama =
    [...DEMO_DRAMAS]
      .sort((a, b) => b.title.length - a.title.length)
      .find((d) => lower.includes(d.title.toLowerCase())) ?? null
  const advertiser = materialAdvertisers(bcId)[0] ?? materialAdvertisers()[0]
  file.source_advertiser_id = advertiser.advertiser_id
  if (file.mime_type.startsWith("image/")) {
    // 合成图片进入图片候选，不制造 video_id 或污染视频目录。
    imageCandidates.push({
      material_id: file.material_id,
      drama_id: drama?.id ?? "",
      file_name_snapshot: file.file_name,
      created_at: isoAt(now()),
      content_digest: "synthetic-demo-upload",
      enabled: true,
      cross_drama_allowed: false,
    })
    return
  }
  DEMO_MATERIALS.unshift({
    id: file.material_id,
    drama_id: drama?.id ?? "",
    index: drama
      ? DEMO_MATERIALS.filter((m) => m.drama_id === drama.id).length + 1
      : 1,
    file_name: file.file_name,
    duration_seconds: 60 + Math.floor(Math.random() * 60),
    size_bytes: file.size,
    advertiser_id: advertiser.advertiser_id,
    video_id: demoNumericId(nextId(), "v10044g50000"),
    uploaded_at: isoAt(now()),
    cover_url: demoCover(
      drama ?? { title: file.file_name.replace(/\.[^.]+$/, ""), color: "#666" },
      "新上传",
    ),
  })
}
