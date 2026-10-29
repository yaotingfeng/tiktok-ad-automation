// 固定代表性演示：单共享组、每广告最多六个完整轮播、单文案；不是平台资格证明。
import type {
  FrozenAd,
  FrozenCreativeUnit,
  FrozenGroup,
  PreviewUnit,
} from "@/client"
import { demoUuid } from "../runtime/clock"
import {
  imageCandidates,
  imageState,
  imageUnits,
} from "./image-composition-state"
import type { DemoDraft } from "./builds-data"
import { DEMO_DRAMAS, DEMO_ADVERTISERS } from "./world"

export interface DemoImageUnit {
  preview: PreviewUnit
  group: FrozenGroup
  ads: FrozenAd[]
}
export const imageDeliveries = new Map<string, DemoImageUnit[]>()
export function findImageUnit(id: string) {
  for (const [previewId, units] of imageDeliveries)
    for (const unit of units)
      if (unit.preview.unit_id === id) return { previewId, unit }
}
export function freezeImageDemo(draft: DemoDraft, previewId: string) {
  const state = imageState(draft.draft_id)
  const units: DemoImageUnit[] = []
  for (const drama of DEMO_DRAMAS.filter((d) =>
    draft.drama_lines.includes(d.title),
  )) {
    const creatives: FrozenCreativeUnit[] = (
      imageUnits.get(draft.draft_id) ?? []
    )
      .filter((u) => u.drama_id === drama.id)
      .map((u) => ({
        unit_id: u.id,
        kind: "CAROUSEL",
        generation: state.active_generation,
        content_revision: u.revision ?? 1,
        music_revision: u.music_revision ?? 1,
        content_digest: u.content_digest,
        music_id: u.music_id ?? null,
        music_snapshot: structuredClone(u.music_snapshot ?? {}),
        members: u.ordered_material_ids.map((id) => ({
          material_id: id,
          content_digest: "synthetic-demo",
          file_name:
            imageCandidates.find((c) => c.material_id === id)
              ?.file_name_snapshot ?? "演示图片.jpg",
        })),
      }))
    for (const account of DEMO_ADVERTISERS.filter((a) =>
      draft.account_lines.includes(a.advertiser_id),
    )) {
      const ads: FrozenAd[] = []
      for (let i = 0; i < creatives.length; i += 6) {
        const selected = structuredClone(creatives.slice(i, i + 6))
        ads.push({
          ad_id: demoUuid(),
          base_ad_no: ads.length + 1,
          creative_no: 1,
          name: `模拟图片广告 ${drama.title} ${ads.length + 1}`,
          copy_id: demoUuid(),
          text: "合成演示文案",
          cta_option_ids: ["WATCH_NOW"],
          included: true,
          reason_codes: [],
          creative_units: selected,
          material_ids: selected.flatMap((c) =>
            c.members.map((m) => m.material_id),
          ),
        })
      }
      const ids = [
        ...new Set(
          creatives.flatMap((c) => c.members.map((m) => m.material_id)),
        ),
      ]
      units.push({
        preview: {
          unit_id: demoUuid(),
          drama_id: drama.id,
          title: drama.title,
          advertiser_id: account.advertiser_id,
          campaign_name: `模拟图片批次 · ${drama.title}`,
          readiness: "PREPARING",
          reason_codes: ["carousel_music_unverified"],
          group_count: 1,
          ad_count: ads.length,
          material_count: ids.length,
          unique_material_count: ids.length,
          material_allocation_count: ids.length,
          group_summary: "1 个共享广告组",
          ad_summary: `${ads.length} 条广告`,
          material_summary: `${creatives.length} 个完整轮播`,
          structure_summary: `合成演示 · ${creatives.length} 单元 → ${ads.length} 广告 · M6 · K1`,
          budget: "100.00",
          currency: "USD",
          skipped_material_count: 0,
        },
        group: {
          contract_version: 2,
          group_id: demoUuid(),
          group_no: 1,
          name: "模拟图片共享组",
          material_count: ids.length,
          creative_unit_count: creatives.length,
          ad_count: ads.length,
          sample_material_ids: ids.slice(0, 3),
          material_ids: null,
          ads: null,
        },
        ads,
      })
    }
  }
  imageDeliveries.set(previewId, units)
  return units
}
