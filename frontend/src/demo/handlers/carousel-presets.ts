// 轮播库的本地合成演示；版本复制、加载都会分配独立身份。
import type { CarouselPresetPublic } from "@/client"
import {
  imageState,
  imageUnits,
  bumpImage,
  demoNewUnit,
  exclusions,
} from "../fixtures/image-composition-state"
import { MAIN_BC_ID } from "../fixtures/world"
import { demoUuid } from "../runtime/clock"
import { handle, demoError } from "../runtime/registry"
const versions: CarouselPresetPublic[] = []
function append(value: CarouselPresetPublic, name: string) {
  const result = {
    ...value,
    id: demoUuid(),
    name,
    version: 1 + versions.filter((v) => v.name === name).length,
    ordered_material_ids: [...value.ordered_material_ids],
    music_snapshot: { ...value.music_snapshot },
    created_at: new Date().toISOString(),
  }
  versions.push(result)
  return result
}
handle("materials-list_versions", (req) => {
  const rows = versions.filter(
      (v) =>
        v.tenant_id === req.path.tenant_id &&
        v.name.includes(req.query.name ?? ""),
    ),
    offset = req.query.offset ?? 0,
    limit = req.query.limit ?? 50
  return {
    items: rows.slice(offset, offset + limit),
    total: rows.length,
    offset,
    limit,
  }
})
handle(
  "materials-read_version",
  (req) =>
    versions.find((v) => v.id === req.path.version_id) ??
    demoError(404, "carousel_preset_not_found", "演示轮播不存在"),
)
handle("materials-save_version", (req) => {
  const body = req.body!
  imageState(body.draft_id)
  const unit = imageUnits
    .get(body.draft_id)
    ?.find((u) => u.id === body.source_unit_id)
  if (!unit)
    return demoError(404, "image_composition_not_found", "演示创意不存在")
  return append(
    {
      id: demoUuid(),
      tenant_id: req.path.tenant_id,
      source_bc_id: MAIN_BC_ID,
      source_draft_id: body.draft_id,
      source_unit_id: unit.id,
      source_drama_id: unit.drama_id,
      name: body.name,
      version: 1,
      content_digest: unit.content_digest,
      ordered_material_ids: [...unit.ordered_material_ids],
      music_id: unit.music_status === "READY" ? (unit.music_id ?? null) : null,
      music_status:
        unit.music_status === "READY"
          ? "READY"
          : unit.music_status === "NEEDS_SELECTION"
            ? "NEEDS_SELECTION"
            : "PENDING",
      music_snapshot:
        unit.music_status === "READY"
          ? {
              display_name: unit.music_snapshot?.display_name,
              author: unit.music_snapshot?.author,
              duration: unit.music_snapshot?.duration,
            }
          : {},
      created_at: new Date().toISOString(),
    },
    body.name,
  )
})
handle("materials-copy_version", (req) => {
  const source = versions.find((v) => v.id === req.path.version_id)
  return source
    ? append(source, req.body!.name)
    : demoError(404, "carousel_preset_not_found", "演示轮播不存在")
})
handle("materials-load_version", (req) => {
  const source = versions.find((v) => v.id === req.path.version_id),
    body = req.body!
  if (!source)
    return demoError(404, "carousel_preset_not_found", "演示轮播不存在")
  imageState(body.draft_id)
  const rows = imageUnits.get(body.draft_id)!
  if (
    source.ordered_material_ids.some((id) =>
      exclusions.get(body.draft_id)?.has(id),
    )
  )
    return demoError(409, "image_candidate_unavailable", "请先恢复已排除图片")
  if (
    !body.allow_reuse &&
    rows.some((u) =>
      u.ordered_material_ids.some((id) =>
        source.ordered_material_ids.includes(id),
      ),
    )
  )
    return demoError(
      409,
      "image_reuse_confirmation_required",
      "请明确确认图片复用",
    )
  const unit = {
    ...demoNewUnit(body.drama_id, source.ordered_material_ids),
    position: 1 + rows.filter((u) => u.drama_id === body.drama_id).length,
    music_id: source.music_id,
    music_status: source.music_status,
    music_snapshot: { ...source.music_snapshot },
    music_protected: source.music_status === "READY",
  }
  rows.push(unit)
  return { revision: bumpImage(body.draft_id), unit }
})
