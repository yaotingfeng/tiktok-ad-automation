import type { CompositionPlanPublic } from "../src/client"
import { expect, test } from "@playwright/test"
import { buildsBoundary, BC, T, D } from "./utils/buildsBoundary"

test("图片整批规则先查看差异，取消不写入组合", async ({ page }) => {
  const { summary, requests } = await buildsBoundary(page)
  Object.assign(summary, { media_kind: "CAROUSEL", structure_override: {} })
  await page.route("**/image-composition", (route) =>
    route.fulfill({
      json: {
        revision: 1,
        active_generation: 0,
        applied_rule_revision: null,
        snapshot_id: D,
        rule: {},
        drama_patches: {},
        candidate_build: null,
        run: null,
        music_counts: {},
      },
    }),
  )
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await expect(page.getByText("整批图片规则", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "查看生成结果", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "生成搭建预览", exact: true }),
  ).toBeDisabled()
  expect(requests.some((r) => r.path.endsWith("/apply"))).toBe(false)
})

async function imageBoundary(page: import("@playwright/test").Page) {
  const base = await buildsBoundary(page)
  Object.assign(base.summary, {
    media_kind: "CAROUSEL",
    structure_override: {},
  })
  const rule = {
    mode: "SEQUENTIAL",
    images_per_unit: 5,
    order: "NATURAL_NAME",
    remainder: "KEEP",
    music_mode: "RECOMMEND",
    fixed_music_id: null,
    single_music_id: null,
  }
  const state = {
    revision: 1,
    active_generation: 1,
    applied_rule_revision: 1,
    snapshot_id: D,
    rule,
    drama_patches: {},
    candidate_build: null as import("../src/client").CandidateProgress | null,
    run: null as Record<string, unknown> | null,
    music_counts: { READY: 12, NEEDS_SELECTION: 1 },
  }
  const unit = {
    id: D,
    drama_id: "88888888-8888-4888-8888-888888888888",
    kind: "CAROUSEL",
    ordered_material_ids: ["image-1", "image-2", "image-3"],
    content_digest: "synthetic",
    position: 1,
    revision: 1,
    music_revision: 1,
    music_request_generation: 1,
    music_status: "READY",
    music_id: "song-original",
    music_snapshot: { display_name: "原曲" },
    composition_protected: true,
    music_protected: true,
  }
  const calls: { suffix: string; body: Record<string, unknown> }[] = []
  let plan: CompositionPlanPublic = {
    plan_id: D,
    base_revision: 2,
    input_digest: "synthetic",
    unit_count: 15,
    added_unit_count: 14,
    removed_unit_count: 0,
    retained_unit_count: 1,
    protected_unit_count: 1,
    music_counts: { PENDING: 14, NEEDS_SELECTION: 1 },
    image_reference_count: 67,
    distinct_image_count: 67,
    reused_image_count: 0,
    conflict_count: 0,
    conflicts: [],
    dramas: [],
    mode: "PRESERVE_MANUAL",
  }
  await page.route("**/image-composition**", async (route) => {
    const url = new URL(route.request().url()),
      suffix = url.pathname.split("image-composition")[1],
      body = route.request().postDataJSON() as Record<string, unknown> | null
    if (body) calls.push({ suffix, body })
    if (suffix === "") return route.fulfill({ json: state })
    if (suffix === "/plans") {
      state.revision++
      Object.assign(state, {
        rule: body!.rule,
        drama_patches: body!.drama_patches,
      })
      plan = {
        ...plan,
        base_revision: state.revision,
        mode: body!.mode as CompositionPlanPublic["mode"],
      }
      return route.fulfill({ json: plan })
    }
    if (suffix.includes("/plans/") && suffix.endsWith("/units"))
      return route.fulfill({
        json: {
          items: [
            {
              ...unit,
              ordered_material_ids: ["image-tail"],
              music_status: "NEEDS_SELECTION",
              music_id: null,
            },
          ],
          total: 15,
          next_cursor: null,
        },
      })
    if (suffix === "/apply") {
      expect(body!.expected_revision).toBe(plan.base_revision)
      state.run = {
        run_id: D,
        plan_id: D,
        generation: 2,
        status: "PENDING",
        processed: 1,
        total: 15,
        cursor: 1,
        error_code: null,
      }
      return route.fulfill({ json: state.run })
    }
    if (suffix.startsWith("/runs/") && suffix.endsWith("/cancel")) {
      state.run = { ...state.run, status: "CANCELLED" }
      return route.fulfill({ json: state.run })
    }
    if (suffix.startsWith("/runs/")) return route.fulfill({ json: state.run })
    if (suffix.endsWith("/differences"))
      return route.fulfill({
        json: {
          items: plan.conflicts,
          total: plan.conflict_count,
          next_cursor: null,
        },
      })
    if (suffix === "/dramas")
      return route.fulfill({
        json: {
          items: [
            {
              drama_id: unit.drama_id,
              link_id: D,
              title: "完整剧名1",
              first_line: 1,
              material_state: "ready",
              matched_count: 61,
              candidate_count: 61,
              excluded_count: 0,
              unused_count: 0,
              unit_count: 13,
              ready_count: 12,
              selection_count: 1,
              pending_count: 0,
              failed_count: 0,
              protected_count: 1,
              reference_count: 61,
              used_image_count: 61,
            },
          ],
          total: 1,
          next_cursor: null,
        },
      })
    if (suffix === "/units")
      return route.fulfill({
        json: { items: [unit], total: 1, next_cursor: null },
      })
    if (suffix === "/edits") {
      plan.conflict_count = 0
      plan.conflicts = []
      const command = body!.command as {
        kind: string
        material_ids?: string[]
        composition_protected?: boolean
        music_protected?: boolean
      }
      if (command.material_ids) unit.ordered_material_ids = command.material_ids
      if (command.kind === "SET_IMAGES") unit.revision++
      if (command.composition_protected !== undefined)
        unit.composition_protected = command.composition_protected
      if (command.music_protected !== undefined)
        unit.music_protected = command.music_protected
      state.revision++
      return route.fulfill({
        json: {
          revision: state.revision,
          affected_units: [D],
          pending_music_ids: [],
        },
      })
    }
    return route.fulfill({ json: { items: [], total: 0, next_cursor: null } })
  })
  await page.route("**/music/search?**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            music_id: "song-new",
            display_name: "新曲",
            author: "演示",
            duration: 30,
            preview_url: null,
          },
        ],
        next_cursor: null,
        truncated: false,
      },
    }),
  )
  await page.route("**/music/units/*", (route) => {
    calls.push({ suffix: "music", body: route.request().postDataJSON() })
    unit.music_revision++
    unit.music_id = "song-new"
    unit.music_snapshot.display_name = "新曲"
    state.revision++
    return route.fulfill({
      json: {
        revision: state.revision,
        music_revision: unit.music_revision,
        request_generation: 2,
        status: "READY",
        job_ids: [],
      },
    })
  })
  await page.route("**/materials/*/original-preview", (route) =>
    route.fulfill({
      json: {
        url: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='80' height='80'%3E%3Crect width='80' height='80' fill='gray'/%3E%3C/svg%3E",
      },
    }),
  )
  return {
    ...base,
    state,
    unit,
    calls,
    conflict: () => {
      Object.assign(plan, {
        conflict_count: 1,
        conflicts: [
          {
            code: "PROTECTED_MUSIC_COMPOSITION_CHANGED",
            unit_id: D,
            drama_id: unit.drama_id,
            material_ids: [],
          },
        ],
      })
    },
  }
}

test("61图尾部单图待配乐、取消差异不变，明确应用并刷新恢复生成中", async ({
  page,
}) => {
  const env = await imageBoundary(page)
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "查看生成结果", exact: true })
  await expect(dialog.getByText("共 15 个创意", { exact: false })).toBeVisible()
  await expect(dialog.getByText(/1 张 · 待选音乐/)).toBeVisible()
  await dialog.getByRole("button", { name: "返回修改" }).click()
  expect(env.calls.filter((c) => c.suffix === "/apply")).toHaveLength(0)
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  await dialog.getByRole("button", { name: "应用并生成" }).click()
  await expect(page.getByText("生成中 1/15", { exact: false })).toBeVisible()
  await page.reload()
  await expect(page.getByText("生成中 1/15", { exact: false })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "生成搭建预览", exact: true }),
  ).toBeDisabled()
})

test("图片编辑取消与未保存离开、选曲取消不写入，确认音乐才保存", async ({
  page,
}) => {
  const env = await imageBoundary(page)
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await sheet.getByRole("button", { name: "图片 2 前移" }).click()
  await sheet.getByRole("button", { name: "完成", exact: true }).click()
  await expect(
    page.getByRole("dialog", { name: "有未保存图片调整" }),
  ).toBeVisible()
  await page.getByRole("button", { name: "继续编辑", exact: true }).click()
  await sheet.getByRole("button", { name: "取消图片调整" }).click()
  expect(env.calls.filter((c) => c.suffix === "/edits")).toHaveLength(0)
  await sheet.getByRole("button", { name: "换音乐", exact: true }).click()
  let picker = page.getByRole("dialog", { name: "选择音乐", exact: true })
  await picker.getByLabel("搜索音乐", { exact: true }).fill("新")
  await picker.getByRole("button", { name: "搜索", exact: true }).click()
  await picker.getByRole("button", { name: "新曲 · 演示" }).click()
  await picker.getByRole("button", { name: "取消", exact: true }).click()
  expect(env.calls.filter((c) => c.suffix === "music")).toHaveLength(0)
  await sheet.getByRole("button", { name: "换音乐", exact: true }).click()
  picker = page.getByRole("dialog", { name: "选择音乐", exact: true })
  await picker.getByLabel("搜索音乐", { exact: true }).fill("新")
  await picker.getByRole("button", { name: "搜索", exact: true }).click()
  await picker.getByRole("button", { name: "新曲 · 演示" }).click()
  await picker.getByRole("button", { name: "使用这首音乐" }).click()
  await expect(sheet.getByText("新曲", { exact: true })).toBeVisible()
  expect(env.calls.filter((c) => c.suffix === "music")).toHaveLength(1)
})

test("逐剧字段覆盖和恢复整批配置需重新试算", async ({ page }) => {
  const env = await imageBoundary(page)
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await sheet.getByRole("button", { name: "本剧规则", exact: true }).click()
  await sheet.getByLabel("每轮播张数", { exact: true }).fill("4")
  await sheet.getByRole("button", { name: "完成", exact: true }).click()
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  const preview = env.calls.find((c) => c.suffix === "/plans")!
  expect(preview.body.drama_patches).toEqual({
    "88888888-8888-4888-8888-888888888888": { images_per_unit: 4 },
  })
  await page.getByRole("button", { name: "返回修改", exact: true }).click()
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  await sheet.getByRole("button", { name: "本剧规则", exact: true }).click()
  await sheet.getByRole("button", { name: "恢复整批配置" }).click()
  await sheet.getByRole("button", { name: "完成", exact: true }).click()
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  expect(
    env.calls.filter((c) => c.suffix === "/plans").slice(-1)[0]!.body
      .drama_patches,
  ).toEqual({})
})

test("保护冲突须明确解除，默认保留手工修改", async ({ page }) => {
  const env = await imageBoundary(page)
  env.conflict()
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "查看生成结果", exact: true })
  await expect(
    dialog.getByRole("button", { name: "应用并生成" }),
  ).toBeDisabled()
  expect(env.calls.find((c) => c.suffix === "/plans")!.body.mode).toBe(
    "PRESERVE_MANUAL",
  )
  await dialog.getByRole("button", { name: "确认解除音乐保护" }).click()
  expect(env.unit.music_protected).toBe(false)
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  await expect(dialog.getByRole("button", { name: "应用并生成" })).toBeEnabled()
})

test("整批改曲通过差异应用，失败配乐可明确重试", async ({ page }) => {
  const env = await imageBoundary(page)
  env.unit.music_status = "FAILED"
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("radio", { name: "整批指定一首" }).click()
  await page.getByRole("button", { name: "选择音乐", exact: true }).click()
  const picker = page.getByRole("dialog", { name: "选择音乐", exact: true })
  await picker.getByLabel("搜索音乐", { exact: true }).fill("新")
  await picker.getByRole("button", { name: "搜索", exact: true }).click()
  await picker.getByRole("button", { name: "新曲 · 演示" }).click()
  await picker.getByRole("button", { name: "使用这首音乐" }).click()
  await page.getByRole("button", { name: "查看生成结果", exact: true }).click()
  expect(env.calls.find((c) => c.suffix === "/plans")!.body.rule).toMatchObject(
    { music_mode: "FIXED", fixed_music_id: "song-new" },
  )
  await page.getByRole("button", { name: "返回修改", exact: true }).click()
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await expect(
    sheet.getByRole("button", { name: "重新推荐", exact: true }),
  ).toHaveCount(0)
  await sheet.getByRole("button", { name: "重试失败配乐" }).click()
  expect(env.calls.find((c) => c.suffix === "music")!.body.action).toBe("RETRY")
})

test("后台配乐更新保留未保存图片顺序，内容变化明确阻断覆盖", async ({
  page,
}) => {
  const env = await imageBoundary(page)
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page
    .getByRole("button", { name: "查看与调整轮播", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: /查看与调整轮播/ })
  await sheet.getByRole("button", { name: "图片 2 前移" }).click()
  env.unit.music_revision++
  await page.waitForResponse((response) =>
    response.url().includes("image-composition/units"),
  )
  await expect(
    sheet.getByRole("button", { name: "保存图片调整", exact: true }),
  ).toBeEnabled()
  await sheet.getByRole("button", { name: "保存图片调整", exact: true }).click()
  expect(
    env.calls.filter((c) => c.suffix === "/edits").slice(-1)[0]!.body.command,
  ).toMatchObject({ material_ids: ["image-2", "image-1", "image-3"] })
  await sheet.getByRole("button", { name: "图片 2 前移" }).click()
  env.unit.revision++
  await page.waitForResponse((response) =>
    response.url().includes("image-composition/units"),
  )
  await expect(
    sheet.getByText(
      "图片已被其他操作更新。请取消本地调整并核对最新内容后再编辑。",
      { exact: true },
    ),
  ).toBeVisible()
  await expect(
    sheet.getByRole("button", { name: "保存图片调整", exact: true }),
  ).toBeDisabled()
  await sheet.getByRole("button", { name: "取消图片调整" }).click()
  await sheet.getByRole("button", { name: "图片 2 前移" }).click()
  await expect(
    sheet.getByRole("button", { name: "保存图片调整", exact: true }),
  ).toBeEnabled()
})

test("取消图片匹配后不沿用轮询缓存，当前组合保留", async ({ page }) => {
  const { state } = await imageBoundary(page)
  let polls = 0
  await page.route("**/image-composition/candidates/**", async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith("/refresh")) {
      state.candidate_build = {
        snapshot_id: D,
        revision: 1,
        generation: 2,
        status: "PENDING",
        processed: 10,
        candidate_count: 10,
        completed_drama_count: 0,
        total_drama_count: 2,
        added: null,
        removed: null,
        error_code: null,
        cutoff: "2026-10-29T00:00:00Z",
      }
    } else if (path.endsWith("/cancel")) {
      state.candidate_build = { ...state.candidate_build!, status: "CANCELLED" }
    } else polls++
    return route.fulfill({ json: state.candidate_build })
  })
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "刷新匹配图片" }).click()
  await expect.poll(() => polls).toBeGreaterThan(0)
  await page.getByRole("button", { name: "取消匹配" }).click()
  await expect(page.getByText("图片匹配已停止，可重新刷新。")).toBeVisible()
  await expect(page.getByRole("button", { name: "取消匹配" })).toHaveCount(0)
  expect(state.active_generation).toBe(1)
})
