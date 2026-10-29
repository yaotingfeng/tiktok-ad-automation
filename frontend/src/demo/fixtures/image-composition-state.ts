// 虚构图片草稿，仅供演示路由使用；不包含平台或最终广告数量算法。
import type {
  CompositionPlanPublic,
  CompositionStatePublic,
  ImageUnitPublic,
  ImageCandidatePublic,
} from "@/client"
import { imageRuleDefault } from "@/features/builds/imageCompositionApi"
import { DEMO_DRAMAS } from "./world"
import { demoDrafts } from "./builds-data"
import { demoUuid } from "../runtime/clock"
export const imageStates = new Map<string, CompositionStatePublic>()
export const imageUnits = new Map<string, ImageUnitPublic[]>()
export const imagePlans = new Map<string, CompositionPlanPublic>()
export const imageState = (id: string) => {
  let state = imageStates.get(id)
  if (!state) {
    state = {
      revision: 1,
      active_generation: 1,
      applied_rule_revision: 1,
      snapshot_id: demoUuid(910),
      rule: { ...imageRuleDefault, images_per_unit: 5 },
      drama_patches: {},
      candidate_build: null,
      run: null,
      music_counts: { READY: 13, NEEDS_SELECTION: 1 },
    }
    imageStates.set(id, state)
    imageUnits.set(
      id,
      Array.from({ length: 14 }, (_, i) => ({
        id: demoUuid(1000 + i),
        drama_id: DEMO_DRAMAS[i < 13 ? 0 : 1].id,
        kind: "CAROUSEL",
        ordered_material_ids: Array.from({ length: i === 12 ? 1 : 5 }, (_, j) =>
          demoUuid(1100 + i * 5 + j),
        ),
        content_digest: "synthetic-demo",
        position: i < 13 ? i + 1 : 1,
        music_id: i === 12 ? null : "demo-song",
        music_status: i === 12 ? "NEEDS_SELECTION" : "READY",
        music_snapshot:
          i === 12 ? {} : { display_name: "夜幕轻奏", author: "演示曲库" },
        revision: 1,
        music_revision: 1,
        music_request_generation: 1,
        composition_protected: i === 0,
        music_protected: i === 0,
      })),
    )
  }
  state.music_counts = {}
  for (const unit of imageUnits.get(id) ?? [])
    state.music_counts[unit.music_status ?? "PENDING"] =
      (state.music_counts[unit.music_status ?? "PENDING"] ?? 0) + 1
  return state
}

export const imageCandidates: ImageCandidatePublic[] = DEMO_DRAMAS.slice(
  0,
  2,
).flatMap((drama, index) =>
  Array.from({ length: index === 0 ? 61 : 5 }, (_, i) => ({
    material_id: demoUuid(1100 + (index === 0 ? i : 65 + i)),
    drama_id: drama.id,
    file_name_snapshot: `${drama.title}_${i + 1}.jpg`,
    created_at: "2026-10-29T00:00:00Z",
    content_digest: "synthetic-demo",
    enabled: true,
    cross_drama_allowed: false,
  })),
)
export const exclusions = new Map<string, Set<string>>()
export function bumpImage(id: string, keepApplied = true) {
  const state = imageState(id),
    wasApplied = state.revision === state.applied_rule_revision
  state.revision++
  if (keepApplied && wasApplied) state.applied_rule_revision = state.revision
  const draft = demoDrafts.find((d) => d.draft_id === id)
  if (draft) draft.revision = state.revision
  return state.revision
}
export function demoNewUnit(dramaId: string, ids: string[]): ImageUnitPublic {
  return {
    id: demoUuid(),
    drama_id: dramaId,
    kind: "CAROUSEL",
    ordered_material_ids: [...ids],
    content_digest: "synthetic-demo",
    position: 1,
    revision: 1,
    music_revision: 0,
    music_request_generation: 0,
    music_id: null,
    music_status: ids.length === 1 ? "NEEDS_SELECTION" : "PENDING",
    music_snapshot: {},
    composition_protected: true,
    music_protected: false,
  }
}
