import { BuildsService, type ImageRule } from "@/client"
export const imageRuleDefault: ImageRule = {
  mode: "SEQUENTIAL",
  images_per_unit: 3,
  order: "NATURAL_NAME",
  remainder: "KEEP",
  music_mode: "RECOMMEND",
  fixed_music_id: null,
  single_music_id: null,
}
export const imagePath = (tenantId: string, draftId: string) => ({
  tenant_id: tenantId,
  draft_id: draftId,
})
export const imageKey = (tenantId: string, draftId: string) =>
  ["image-composition", tenantId, draftId] as const
export const change = (revision: number) => ({
  request_id: crypto.randomUUID(),
  expected_revision: revision,
})
export const imageApi = BuildsService
export const musicLabel: Record<string, string> = {
  READY: "已选音乐",
  NEEDS_SELECTION: "待选音乐",
  PENDING: "配乐中",
  RUNNING: "配乐中",
  FAILED: "配乐失败",
}
