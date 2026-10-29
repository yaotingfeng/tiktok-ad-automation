// 全部为虚构本地演示状态；不调用平台，也不作为投放资格证明。
import type { CompositionPlanPublic, ImageDifference } from "@/client"
import {
  imageState as get,
  imageUnits as units,
  imagePlans as plans,
  imageCandidates,
  exclusions,
  bumpImage,
  demoNewUnit,
} from "../fixtures/image-composition-state"
import { demoDrafts } from "../fixtures/builds-data"
import { DEMO_DRAMAS } from "../fixtures/world"
import { demoUuid } from "../runtime/clock"
import { handle, demoError } from "../runtime/registry"
const demoLosses = new Map<string, ImageDifference[]>()
handle("builds-composition_state", (req) => get(req.path.draft_id))
handle("builds-units", (req) => {
  get(req.path.draft_id)
  const rows = (units.get(req.path.draft_id) ?? []).filter(
    (u) => !req.query.drama_id || u.drama_id === req.query.drama_id,
  )
  const offset = Number(req.query.cursor ?? 0),
    limit = req.query.limit ?? 20
  return {
    items: rows.slice(offset, offset + limit),
    total: rows.length,
    next_cursor: offset + limit < rows.length ? String(offset + limit) : null,
  }
})
handle("builds-image_dramas", (req) => {
  get(req.path.draft_id)
  const rows = DEMO_DRAMAS.slice(0, 2)
    .map((drama, index) => {
      const group = (units.get(req.path.draft_id) ?? []).filter(
          (u) => u.drama_id === drama.id,
        ),
        candidates = imageCandidates.filter((c) => c.drama_id === drama.id),
        used = new Set(group.flatMap((u) => u.ordered_material_ids))
      return {
        drama_id: drama.id,
        link_id: demoUuid(1200 + index),
        title: drama.title,
        first_line: index + 1,
        material_state: "ready",
        matched_count: candidates.length,
        candidate_count: candidates.length,
        excluded_count: candidates.filter((c) =>
          exclusions.get(req.path.draft_id)?.has(c.material_id),
        ).length,
        unused_count: candidates.filter((c) => !used.has(c.material_id)).length,
        unit_count: group.length,
        ready_count: group.filter((u) => u.music_status === "READY").length,
        selection_count: group.filter(
          (u) => u.music_status === "NEEDS_SELECTION",
        ).length,
        pending_count: group.filter((u) =>
          ["PENDING", "RUNNING"].includes(u.music_status ?? ""),
        ).length,
        failed_count: group.filter((u) => u.music_status === "FAILED").length,
        protected_count: group.filter(
          (u) => u.composition_protected || u.music_protected,
        ).length,
        reference_count: group.flatMap((u) => u.ordered_material_ids).length,
        used_image_count: used.size,
      }
    })
    .filter((d) =>
      req.query.issue === "unused"
        ? d.unused_count > 0
        : req.query.issue === "protected"
          ? d.protected_count > 0
          : req.query.issue === "music"
            ? d.selection_count + d.pending_count + d.failed_count > 0
            : true,
    )

  return { items: rows, total: rows.length, next_cursor: null }
})
handle("builds-preview", (req) => {
  const state = get(req.path.draft_id)
  state.revision++
  state.rule = req.body!.rule
  state.drama_patches = req.body!.drama_patches ?? {}
  const draft = demoDrafts.find((d) => d.draft_id === req.path.draft_id)
  if (draft) draft.revision = state.revision
  const plan: CompositionPlanPublic = {
    plan_id: demoUuid(),
    base_revision: state.revision,
    input_digest: "synthetic",
    unit_count: 14,
    added_unit_count: 13,
    removed_unit_count: 0,
    retained_unit_count: 1,
    protected_unit_count: 1,
    music_counts: { PENDING: 13, NEEDS_SELECTION: 1 },
    image_reference_count: 66,
    distinct_image_count: 66,
    reused_image_count: 0,
    conflicts: [],
    conflict_count: 0,
    dramas: DEMO_DRAMAS.slice(0, 2).map((drama) => ({
      drama_id: drama.id,
      drama_title: drama.title,
      rule_digest: "synthetic",
      candidate_digest: "synthetic",
      retained_ids_count: 0,
      removed_ids_count: 0,
      excluded_images_count: 0,
      warnings_count: 0,
      conflicts_count: 0,
      protection_losses_count:
        req.body!.mode === "REPLACE_ALL"
          ? (units.get(req.path.draft_id) ?? []).filter(
              (unit) =>
                unit.drama_id === drama.id &&
                (unit.composition_protected || unit.music_protected),
            ).length
          : 0,
    })),
    mode: req.body!.mode ?? "PRESERVE_MANUAL",
  }
  demoLosses.set(
    plan.plan_id,
    plan.mode === "REPLACE_ALL"
      ? (units.get(req.path.draft_id) ?? [])
          .filter((unit) => unit.composition_protected || unit.music_protected)
          .map((unit) => ({
            drama_id: unit.drama_id,
            drama_title: DEMO_DRAMAS.find((drama) => drama.id === unit.drama_id)
              ?.title,
            unit_id: unit.id,
            unit_position: unit.position,
            composition_protected: unit.composition_protected,
            music_protected: unit.music_protected,
            music_title: unit.music_snapshot?.display_name,
            materials: unit.ordered_material_ids.map((id) => ({
              material_id: id,
              file_name:
                imageCandidates.find(
                  (candidate) => candidate.material_id === id,
                )?.file_name_snapshot ?? "演示图片",
            })),
          }))
      : [],
  )
  plans.set(plan.plan_id, plan)
  return plan
})
handle(
  "builds-plan",
  (req) =>
    plans.get(req.path.plan_id) ??
    demoError(404, "image_composition_not_found", "演示试算不存在"),
)
handle("builds-plan_units", (req) => ({
  items: (units.get(req.path.draft_id) ?? []).map((unit) => ({
    ...unit,
    drama_title: DEMO_DRAMAS.find((drama) => drama.id === unit.drama_id)?.title,
  })),
  total: units.get(req.path.draft_id)?.length ?? 0,
  next_cursor: null,
}))
handle("builds-differences", (req) => {
  const items =
    req.query.kind === "protection_losses"
      ? (demoLosses.get(req.path.plan_id) ?? [])
      : []
  return { items, total: items.length, next_cursor: null }
})
handle("builds-apply", (req) => {
  const state = get(req.path.draft_id)
  state.run = {
    run_id: demoUuid(),
    plan_id: req.body!.plan_id,
    generation: state.active_generation + 1,
    status: "PENDING",
    processed: 0,
    total: 14,
    cursor: 0,
    error_code: null,
  }
  return state.run
})
handle("builds-progress", (req) => {
  const state = get(req.path.draft_id)
  if (!state.run)
    return demoError(404, "image_composition_not_found", "演示任务不存在")
  if (state.run.status === "PENDING") {
    const processed = Math.min(14, state.run.processed + 7)
    state.run = {
      ...state.run,
      processed,
      cursor: processed,
      status: processed === 14 ? "APPLIED" : "PENDING",
    }
    if (state.run.status === "APPLIED") {
      state.active_generation = state.run.generation
      state.applied_rule_revision = state.revision
    }
  }
  return state.run
})
handle("builds-cancel", (req) => {
  const state = get(req.path.draft_id)
  if (!state.run)
    return demoError(404, "image_composition_not_found", "演示任务不存在")
  state.run = { ...state.run, status: "CANCELLED" }
  return state.run
})
handle("builds-edit", (req) => {
  const id = req.path.draft_id,
    state = get(id),
    command = req.body!.command,
    rows = units.get(id)!
  if (req.body!.expected_revision !== state.revision)
    return demoError(409, "draft_revision_conflict", "演示草稿已更新，请刷新")
  const unit =
    "unit_id" in command
      ? rows.find((u) => u.id === command.unit_id)
      : undefined
  let affected: string[] = []
  if (unit && command.kind === "SET_IMAGES") {
    unit.ordered_material_ids = command.material_ids
    unit.revision = (unit.revision ?? 1) + 1
    unit.composition_protected = true
    unit.music_snapshot = { ...unit.music_snapshot, images_changed: true }
    affected = [unit.id]
  } else if (unit && command.kind === "SET_PROTECTION") {
    if (command.composition_protected != null)
      unit.composition_protected = command.composition_protected
    if (command.music_protected != null)
      unit.music_protected = command.music_protected
    affected = [unit.id]
  } else if (unit && command.kind === "DUPLICATE") {
    const copy = {
      ...unit,
      id: demoUuid(),
      ordered_material_ids: [...unit.ordered_material_ids],
      composition_protected: true,
    }
    rows.push(copy)
    affected = [copy.id]
  } else if (unit && command.kind === "DELETE") {
    units.set(
      id,
      rows.filter((u) => u.id !== unit.id),
    )
    const excluded = exclusions.get(id) ?? new Set<string>()
    for (const materialId of unit.ordered_material_ids)
      if (
        !units.get(id)!.some((u) => u.ordered_material_ids.includes(materialId))
      )
        excluded.add(materialId)
    exclusions.set(id, excluded)
    affected = [unit.id]
  } else if (unit && command.kind === "SPLIT") {
    if (unit.music_protected)
      return demoError(409, "image_composition_conflict", "请先解除音乐保护")
    const positions = [
        0,
        ...command.split_positions,
        unit.ordered_material_ids.length,
      ],
      next = positions
        .slice(1)
        .map((end, i) =>
          demoNewUnit(
            unit.drama_id,
            unit.ordered_material_ids.slice(positions[i], end),
          ),
        )
    units.set(id, [...rows.filter((u) => u.id !== unit.id), ...next])
    affected = next.map((u) => u.id)
  } else if (command.kind === "MERGE") {
    const selected = command.unit_ids
      .map((identity) => rows.find((u) => u.id === identity))
      .filter((u) => !!u)
    if (selected.some((u) => u.music_protected))
      return demoError(409, "image_composition_conflict", "请先解除音乐保护")
    const images = selected.flatMap((u) => u.ordered_material_ids)
    if (
      selected.length < 2 ||
      images.length > 35 ||
      new Set(images).size !== images.length
    )
      return demoError(
        422,
        "image_edit_invalid",
        "请选择不重复且合计不超过35张的轮播",
      )
    const merged = demoNewUnit(selected[0].drama_id, images)
    units.set(id, [
      ...rows.filter((u) => !command.unit_ids.includes(u.id)),
      merged,
    ])
    affected = [merged.id]
  } else if (command.kind === "CREATE") {
    const created = demoNewUnit(command.drama_id, command.material_ids)
    rows.push(created)
    affected = [created.id]
  } else if (command.kind === "RESTORE_CANDIDATES") {
    for (const identity of command.material_ids)
      exclusions.get(id)?.delete(identity)
  } else return demoError(422, "image_edit_invalid", "演示创意或操作不存在")
  const positions: Record<string, number> = {}
  for (const row of units.get(id)!) {
    positions[row.drama_id] = (positions[row.drama_id] ?? 0) + 1
    row.position = positions[row.drama_id]
  }
  return {
    revision: bumpImage(id),
    affected_units: affected,
    pending_music_ids: units
      .get(id)!
      .filter((u) => affected.includes(u.id) && u.music_status !== "READY")
      .map((u) => u.id),
  }
})
handle("builds-search_music", () => ({
  items: [
    {
      music_id: "demo-song",
      display_name: "夜幕轻奏",
      author: "演示曲库",
      duration: 30,
      preview_url: null,
    },
    {
      music_id: "demo-song-2",
      display_name: "晨光序曲",
      author: "演示曲库",
      duration: 24,
      preview_url: null,
    },
  ],
  next_cursor: null,
  truncated: false,
}))
handle("builds-read_music", (req) => ({
  items: req.body!.music_ids.map((id) => ({
    music_id: id,
    display_name: id === "demo-song" ? "夜幕轻奏" : "晨光序曲",
    author: "演示曲库",
    duration: 30,
    preview_url: null,
  })),
  next_cursor: null,
  truncated: false,
}))
handle("builds-request_music", (req) => {
  const state = get(req.path.draft_id),
    unit = units.get(req.path.draft_id)?.find((u) => u.id === req.path.unit_id)
  if (!unit)
    return demoError(404, "image_composition_not_found", "演示创意不存在")
  unit.music_id = req.body!.music_id ?? "demo-song"
  unit.music_revision = (unit.music_revision ?? 0) + 1
  unit.music_status = "READY"
  unit.music_protected =
    req.body!.action === "SET_MANUAL" || unit.music_protected
  unit.music_snapshot = {
    display_name: unit.music_id === "demo-song" ? "夜幕轻奏" : "晨光序曲",
  }
  bumpImage(req.path.draft_id)
  return {
    job_ids: [],
    revision: state.revision,
    music_revision: unit.music_revision,
    request_generation: 1,
    status: "READY" as const,
  }
})
handle("builds-update_image_structure", (req) => {
  const state = get(req.path.draft_id)
  if (state.run?.status === "PENDING")
    return demoError(
      409,
      "image_composition_busy",
      "图片正在应用，请等待或取消",
    )
  bumpImage(req.path.draft_id)
  const draft = demoDrafts.find((d) => d.draft_id === req.path.draft_id)
  if (draft) {
    draft.structure_override = req.body!.structure_override
    draft.revision = state.revision
  }
  return { draft_id: req.path.draft_id, revision: state.revision }
})
handle("builds-candidates", (req) => {
  const rows = imageCandidates
    .filter((c) => !req.query.drama_id || c.drama_id === req.query.drama_id)
    .map((c) => ({
      ...c,
      enabled: !exclusions.get(req.path.draft_id)?.has(c.material_id),
    }))
  const offset = Number(req.query.cursor ?? 0),
    limit = req.query.limit ?? 50
  return {
    items: rows.slice(offset, offset + limit),
    total: rows.length,
    next_cursor: offset + limit < rows.length ? String(offset + limit) : null,
  }
})
handle("builds-refresh", (req) => {
  const state = get(req.path.draft_id)
  state.candidate_build = {
    snapshot_id: demoUuid(),
    revision: state.revision,
    status: "PENDING",
    generation: 1,
    processed: 0,
    candidate_count: 0,
    completed_drama_count: 0,
    total_drama_count: 2,
    added: null,
    removed: null,
    error_code: null,
    cutoff: new Date().toISOString(),
  }
  return state.candidate_build
})
handle("builds-candidate_progress", (req) => {
  const state = get(req.path.draft_id)
  if (!state.candidate_build)
    return demoError(404, "image_composition_not_found", "演示候选不存在")
  if (state.candidate_build.status === "PENDING") {
    const done = state.candidate_build.processed > 0
    state.candidate_build = {
      ...state.candidate_build,
      status: done ? "READY" : "PENDING",
      processed: done ? 66 : 33,
      candidate_count: done ? 66 : 33,
      completed_drama_count: done ? 2 : 1,
      added: done ? 0 : null,
      removed: done ? 0 : null,
    }
  }
  return state.candidate_build
})
handle("builds-cancel_candidates", (req) => {
  const state = get(req.path.draft_id)
  if (!state.candidate_build)
    return demoError(404, "image_composition_not_found", "演示候选不存在")
  state.candidate_build = { ...state.candidate_build, status: "CANCELLED" }
  return state.candidate_build
})
handle("builds-adopt", (req) => {
  const state = get(req.path.draft_id)
  state.snapshot_id = req.body!.snapshot_id
  state.candidate_build = null
  return {
    revision: bumpImage(req.path.draft_id, false),
    snapshot_id: state.snapshot_id,
  }
})
handle("builds-add_candidates", (req) => {
  // 合成库的本剧图片已全部纳入候选，跨剧选择明确报错，不伪造成功导入。
  if (
    req.body!.material_ids.some(
      (id) =>
        !imageCandidates.some(
          (c) => c.material_id === id && c.drama_id === req.body!.drama_id,
        ),
    )
  )
    return demoError(422, "image_edit_invalid", "该演示仅支持本剧已有图片")
  const state = get(req.path.draft_id)
  return {
    revision: bumpImage(req.path.draft_id, false),
    snapshot_id: state.snapshot_id!,
    added: 0,
  }
})
handle("builds-exclude", (req) => {
  const excluded = exclusions.get(req.path.draft_id) ?? new Set<string>()
  for (const id of req.body!.material_ids) excluded.add(id)
  exclusions.set(req.path.draft_id, excluded)
  return { revision: bumpImage(req.path.draft_id, false) }
})
handle("builds-allocate", (req) => ({
  revision: bumpImage(req.path.draft_id, false),
}))
handle("builds-music_summary", (req) => ({
  active_generation: get(req.path.draft_id).active_generation,
  jobs: {},
  preparations: {},
  preparation_errors: [],
  recent_errors: [],
}))
handle("builds-music_progress", () =>
  demoError(404, "image_composition_not_found", "该合成用例不创建后台音乐任务"),
)

// 仅模拟批量进度；真实分片、恢复与版本栅栏在服务端测试中验证。
const retries = new Map<string, import("@/client").MusicBatchProgress[]>()
handle("builds-retry_selected_music", (req) => {
  const id = req.path.draft_id,
    state = get(id)
  if (req.body!.expected_revision !== state.revision)
    return demoError(409, "draft_revision_conflict", "草稿已变化")
  const result: import("@/client").MusicBatchProgress = {
    run_id: demoUuid(),
    revision: bumpImage(id),
    drama_ids: req.body!.drama_ids,
    status: "PENDING",
    processed: 0,
    retried: 0,
    skipped_protected: 0,
    skipped_recovered: 0,
    error_code: null,
  }
  retries.set(id, [result, ...(retries.get(id) ?? [])].slice(0, 20))
  return result
})
handle("builds-music_retry_batches", (req) => {
  const rows = retries.get(req.path.draft_id) ?? []
  for (const run of rows)
    if (run.status === "PENDING") {
      if (get(req.path.draft_id).revision !== run.revision) {
        run.status = "CANCELLED"
        run.error_code = "draft_revision_conflict"
        continue
      }
      for (const unit of units.get(req.path.draft_id) ?? [])
        if (run.drama_ids.includes(unit.drama_id)) {
          run.processed++
          if (unit.music_protected) run.skipped_protected++
          else if (unit.music_status !== "FAILED") run.skipped_recovered++
          else {
            unit.music_status = "READY"
            unit.music_id = "demo-song"
            run.retried++
          }
        }
      run.status = "READY"
    }
  return rows
})
handle(
  "builds-music_retry_progress",
  (req) =>
    retries.get(req.path.draft_id)?.find((r) => r.run_id === req.path.run_id) ??
    demoError(404, "image_composition_not_found", "演示任务不存在"),
)
