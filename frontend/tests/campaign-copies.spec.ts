import { expect, type Page, test } from "@playwright/test"
import type {
  CopyDraftPublic,
  CopyMutation,
} from "../src/features/campaign-copies/api"
import { configurationError } from "../src/features/campaign-copies/api"
import { adsBoundary, BC_A, BC_B, TENANT } from "./utils/adsBoundary"

const DRAFT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
const url = `/tenants/${TENANT}/campaign-copies/${DRAFT}?bc_id=${BC_A}`
function fixture(): CopyDraftPublic {
  return {
    draft_id: DRAFT,
    bc_id: BC_A,
    revision: 1,
    status: "DRAFT",
    preview_digest: null,
    children: [],
    submission_ids: [],
    error_code: null,
    rows: [1, 2].map((n) => ({
      row_id: `row-${n}`,
      source: {
        advertiser_id: "adv-1",
        campaign_id: `campaign-${n}`,
        name: `来源系列${n}`,
        currency: n === 1 ? "USD" : "EUR",
        budget: "100",
        target_roas: "1.1",
        budget_strategy: "SERIES",
        bid_strategy: "TARGET_ROAS",
        blockers: [],
        targeting: {
          region_mode: "SELECTED",
          region_codes: ["US"],
          languages: ["en"],
          age_groups: [],
          gender: "GENDER_UNLIMITED",
        },
        materials: [
          {
            material_id: `m-${n}-1`,
            file_name: `素材${n}-1.mp4`,
            media_type: "VIDEO",
          },
          {
            material_id: `m-${n}-2`,
            file_name: `素材${n}-2.mp4`,
            media_type: "VIDEO",
          },
        ],
      },
      configuration: {
        target_advertiser_ids: ["adv-1"],
        copies: 1,
        budget: null,
        target_roas: null,
        targeting: null,
        delivery_config: { schedule_mode: "IMMEDIATE" },
        material_ids: [`m-${n}-1`, `m-${n}-2`],
      },
    })),
  }
}
async function boundary(
  page: Page,
  options: {
    viewer?: boolean
    blocker?: boolean
    conflict?: boolean
    loseSubmit?: boolean
    mixed?: boolean
  } = {},
) {
  await adsBoundary(page, { role: options.viewer ? "viewer" : "operator" })
  let draft = fixture()
  if (options.blocker)
    draft.rows[0].source.blockers = ["source_actual_configuration_missing"]
  if (options.mixed) {
    draft.rows[0].source.targeting_mixed = true
    draft.rows[0].source.group_settings = [
      {
        name: "美国组",
        budget: "80",
        target_roas: "1.2",
        targeting: draft.rows[0].source.targeting,
      },
      {
        name: "英国组",
        budget: "120",
        target_roas: "1.5",
        targeting: {
          region_mode: "SELECTED",
          region_codes: ["GB"],
          languages: ["en"],
          age_groups: ["AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55_100"],
          gender: "GENDER_UNLIMITED",
        },
      },
    ]
    draft.rows[0].source.targeting = null
    draft.rows[0].source.budget = null
  }
  const mutations: Array<{
    method: string
    body: CopyMutation["body"]
    action: string
  }> = []
  let reads = 0
  let submitted = false
  await page.route("**/api/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname
    if (path.endsWith("/targeting/regions"))
      return route.fulfill({
        json: {
          state: "READY",
          region_codes: ["US", "GB"],
          verified_account_count: 0,
          unavailable_region_codes: [],
        },
      })
    if (path.endsWith("/accounts/resolve"))
      return route.fulfill({
        json: request
          .postDataJSON()
          .lines.map((line: { raw: string; line_no: number }) => ({
            ...line,
            status: "MATCHED",
            advertiser_id: line.raw,
          })),
      })
    if (path.endsWith("/accounts"))
      return route.fulfill({
        json: {
          items: ["adv-1", "adv-2"].map((id) => ({
            advertiser_id: id,
            name: id === "adv-1" ? "原账户" : "目标账户二",
            bc_id: BC_A,
            currency: "USD",
            timezone: "UTC",
            remote_status: "ENABLE",
            ownership_conflict: false,
            can_build: true,
            can_upload: false,
            permission_state: "READY",
            availability: "AVAILABLE",
            checked_at: null,
          })),
          total: 2,
          next_cursor: null,
        },
      })
    if (!path.includes("/campaign-copies/")) return route.fallback()
    if (request.method() === "GET") {
      reads++
      return route.fulfill({ json: draft })
    }
    const body = request.postDataJSON() as CopyMutation["body"]
    mutations.push({
      method: request.method(),
      body,
      action: path.split("/").slice(-1)[0],
    })
    if (request.method() === "PATCH") {
      if (options.conflict) {
        draft.revision = 2
        draft.rows[0].configuration.budget = "222"
        return route.fulfill({
          status: 409,
          json: { code: "copy_revision_conflict", message: "草稿修订已更新" },
        })
      }
      const patch = body as Extract<CopyMutation, { kind: "save" }>["body"]
      draft = {
        ...draft,
        revision: draft.revision + 1,
        status: "DRAFT",
        rows: draft.rows.map((row) => ({
          ...row,
          configuration:
            patch.rows.find((item) => item.row_id === row.row_id)
              ?.configuration ?? row.configuration,
        })),
      }
    }
    if (path.endsWith("/preview")) {
      draft.status = "READY"
      draft.preview_digest = "a".repeat(64)
      draft.children = draft.rows.flatMap((row) =>
        row.configuration.target_advertiser_ids.flatMap((advertiser_id) =>
          Array.from({ length: row.configuration.copies }, (_, n) => ({
            row_id: row.row_id,
            advertiser_id,
            ordinal: n + 1,
            preview_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            group_settings: row.source.group_settings?.map((group) => ({
              ...group,
              budget: String(Number(group.budget) + 1),
            })),
            submission_id: null,
            status: "READY",
            error_code: null,
          })),
        ),
      )
    }
    if (path.endsWith("/refresh-source")) {
      draft = {
        ...draft,
        revision: draft.revision + 1,
        status: "DRAFT",
        rows: draft.rows.map((row) => ({
          ...row,
          source: { ...row.source, blockers: [] },
        })),
        children: [],
        preview_digest: null,
        error_code: null,
      }
    }
    if (path.endsWith("/submit")) {
      draft.status = "SUBMITTED"
      draft.submission_ids = ["eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"]
      draft.children = draft.children.map((child) => ({
        ...child,
        submission_id: draft.submission_ids[0],
        status: "SUBMITTED",
      }))
      if (options.loseSubmit && !submitted) {
        submitted = true
        return route.abort("failed")
      }
    }
    return route.fulfill({ json: draft })
  })
  return { mutations, reads: () => reads }
}

test("来源读取失败不显示默认最高价值，重新读取后可继续预览", async ({
  page,
}) => {
  const state = await boundary(page, { blocker: true })
  await page.goto(url)
  const row = page.getByTestId("copy-row-row-1")
  await expect(row.getByText("未读取", { exact: true })).toBeVisible()
  await expect(row.getByRole("button", { name: "素材未读取" })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "生成复制预览" }),
  ).toBeDisabled()
  await page.getByRole("button", { name: "重新读取来源", exact: true }).click()
  await expect(
    page.getByText(
      "已重新读取来源，保留已保存的覆盖设置，请核对素材选择后生成预览。",
    ),
  ).toBeVisible()
  expect(state.mutations.at(-1)?.action).toBe("refresh-source")
  await expect(page.getByRole("button", { name: "生成复制预览" })).toBeEnabled()
})

test("复制份数与展开上限按全部来源计算", () => {
  const draft = fixture()
  draft.rows[0].configuration.copies = 21
  expect(configurationError(draft.rows)).toContain("1–20")
  draft.rows[0].configuration.copies = 20
  draft.rows[0].configuration.target_advertiser_ids = Array.from(
    { length: 51 },
    (_, n) => `account-${n}`,
  )
  expect(configurationError(draft.rows)).toContain("1000")
})

test("逐行编辑、批量填值只影响勾选，预览提交覆盖整个复制队列", async ({
  page,
}) => {
  const state = await boundary(page)
  await page.goto(url)
  await expect(
    page.getByRole("heading", { name: "批量复制", exact: true }),
  ).toBeVisible()
  await page.getByLabel("来源系列1 日预算", { exact: true }).fill("150")
  await page.getByLabel("勾选 来源系列1", { exact: true }).check()
  await page.getByRole("button", { name: "复制份数", exact: true }).click()
  await page
    .getByRole("spinbutton", { name: "每账户份数", exact: true })
    .fill("2")
  await page.getByRole("button", { name: "保存设置", exact: true }).click()
  await expect(
    page.getByLabel("来源系列2 每账户份数", { exact: true }),
  ).toHaveValue("1")
  await page.getByRole("button", { name: "生成复制预览", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "创建并启用 3 个新系列", exact: true }),
  ).toBeVisible()
  const saved = state.mutations.find((item) => item.method === "PATCH")!
    .body as Extract<CopyMutation, { kind: "save" }>["body"]
  expect(saved.expected_revision).toBe(1)
  expect(saved.rows[0].configuration.budget).toBe("150")
  expect(
    state.mutations.find((item) => item.action === "preview")!.body
      .expected_revision,
  ).toBe(2)
  await page
    .getByRole("button", { name: "创建并启用 3 个新系列", exact: true })
    .click()
  await expect(page.getByText("已提交搭建", { exact: true })).toBeVisible()
  expect(
    state.mutations.find((item) => item.action === "submit")!.body,
  ).toMatchObject({ expected_revision: 2, preview_digest: "a".repeat(64) })
})

test("账户和素材取消不修改，保存后重新打开回显；定向与排期复用原组件", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1600, height: 1000 })
  await boundary(page)
  await page.goto(url)
  const row = page.getByTestId("copy-row-row-1")
  await expect(row).toBeVisible()
  await page.screenshot({
    path: "../.runtime/campaign-copy/configuration.png",
    fullPage: true,
    animations: "disabled",
  })
  await row.getByRole("button", { name: "1 个账户", exact: true }).click()
  await page
    .getByRole("button", { name: "移除账户 adv-1", exact: true })
    .click()
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await expect(
    row.getByRole("button", { name: "1 个账户", exact: true }),
  ).toBeVisible()
  await row.getByRole("button", { name: "1 个账户", exact: true }).click()
  await page
    .getByRole("dialog", { name: "目标账户", exact: true })
    .getByRole("button", { name: "选择账户", exact: true })
    .click()
  const picker = page.getByRole("dialog", { name: "选择账户", exact: true })
  await picker.getByRole("checkbox", { name: /目标账户二/ }).check()
  await page.screenshot({
    path: "../.runtime/campaign-copy/account-picker.png",
    fullPage: true,
    animations: "disabled",
  })
  await picker.getByRole("button", { name: "确定添加", exact: true }).click()
  await page.getByRole("button", { name: "保存设置", exact: true }).click()
  await row.getByRole("button", { name: "2 个账户", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "移除账户 adv-2", exact: true }),
  ).toBeVisible()
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await row.getByRole("button", { name: "2 条素材", exact: true }).click()
  await expect(page.getByLabel("素材1-2.mp4")).toBeVisible()
  await page.screenshot({
    path: "../.runtime/campaign-copy/materials-sheet.png",
    fullPage: true,
    animations: "disabled",
  })
  await page.getByLabel("素材1-2.mp4").uncheck()
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await row.getByRole("button", { name: "2 条素材", exact: true }).click()
  await expect(page.getByLabel("素材1-2.mp4")).toBeChecked()
  await page.getByLabel("素材1-2.mp4").uncheck()
  await page.getByRole("button", { name: "保存素材", exact: true }).click()
  await row.getByRole("button", { name: "1 条素材", exact: true }).click()
  await expect(page.getByLabel("素材1-2.mp4")).not.toBeChecked()
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await row.getByRole("button", { name: "修改定向", exact: true }).click()
  await expect(
    page.getByRole("combobox", { name: "最低年龄", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("这是国家参考目录", { exact: false }),
  ).toBeVisible()
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await row.getByRole("button", { name: "更多设置", exact: true }).click()
  await page.getByLabel("指定开始和结束时间", { exact: true }).check()
  await expect(page.getByLabel("开始时间", { exact: true })).toBeVisible()
  await expect(page.getByLabel("结束时间", { exact: true })).toBeVisible()
  await expect(page.getByRole("textbox", { name: /文案/ })).toHaveCount(0)
})

test("修订冲突保留本地输入，明确载入新草稿后才继续编辑", async ({ page }) => {
  await boundary(page, { conflict: true })
  await page.goto(url)
  await page.getByLabel("来源系列1 日预算", { exact: true }).fill("150")
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "载入最新草稿（丢弃本页修改）" }),
  ).toBeVisible()
  await expect(
    page.getByLabel("来源系列1 日预算", { exact: true }),
  ).toHaveValue("150")
  await page
    .getByRole("button", { name: "载入最新草稿（丢弃本页修改）" })
    .click()
  await expect(
    page.getByLabel("来源系列1 日预算", { exact: true }),
  ).toHaveValue("222")
})

test("提交响应丢失后重试使用同一请求标识与冻结摘要", async ({ page }) => {
  const state = await boundary(page, { loseSubmit: true })
  await page.goto(url)
  await page.getByRole("button", { name: "生成复制预览", exact: true }).click()
  await page
    .getByRole("button", { name: "创建并启用 2 个新系列", exact: true })
    .click()
  await page.getByRole("button", { name: "重试原请求", exact: true }).click()
  await expect(
    page.getByText("上次请求结果待确认", { exact: true }),
  ).toHaveCount(0)
  const submits = state.mutations.filter((item) => item.action === "submit")
  expect(submits).toHaveLength(2)
  expect(submits[1].body).toEqual(submits[0].body)
})

test("只读角色与来源阻断不能提交，切换已知草稿 BC 不查询错误作用域", async ({
  page,
}) => {
  const state = await boundary(page, { viewer: true, blocker: true })
  await page.goto(url)
  await expect(
    page.getByText("source_actual_configuration_missing", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "生成复制预览", exact: true }),
  ).toBeDisabled()
  await expect(
    page.getByLabel("来源系列1 日预算", { exact: true }),
  ).toBeDisabled()
  const initial = state.reads()
  await page.goto(url.replace(BC_A, BC_B))
  await expect(
    page.getByText("请选择草稿所属的有效 BC", { exact: true }),
  ).toBeVisible()
  expect(state.reads()).toBe(initial)
  expect(state.mutations).toHaveLength(0)
})

test("@dev-server 演示注册处理器按冻结来源生成独立子预览并提交可回读任务", async ({
  page,
}) => {
  await adsBoundary(page)
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  const result = await page.evaluate(async () => {
    const base = "/src/demo"
    const { resolveDemoRoute, DemoReply } = await import(
      `${base}/handlers/index.ts`
    )
    const { demoWorld, MAIN_BC_ID, MAIN_TENANT_ID } = await import(
      `${base}/fixtures/world.ts`
    )
    async function call(
      method: string,
      suffix: string,
      body?: unknown,
      query = {},
    ) {
      const route = resolveDemoRoute(
        method,
        `/api/tenants/${MAIN_TENANT_ID}${suffix}`,
      )
      if (!route?.handler) throw new Error(`Missing demo route: ${suffix}`)
      const output = await route.handler({
        operationId: route.id,
        method,
        path: route.params,
        query,
        body,
        search: new URLSearchParams(),
        headers: {},
      })
      if (output instanceof DemoReply) {
        if (output.status >= 400) throw new Error(JSON.stringify(output.body))
        return output.body
      }
      return output
    }
    const source = demoWorld.launched.find(
      (item: { bc_id: string }) => item.bc_id === MAIN_BC_ID,
    )
    const selection = await call(
      "POST",
      "/ad-selections",
      { mode: "EXPLICIT", row_keys: [`${MAIN_BC_ID}-${source.campaign_id}`] },
      { bc_id: MAIN_BC_ID },
    )
    const created = await call("POST", "/campaign-copies", {
      bc_id: MAIN_BC_ID,
      selection_id: selection.selection_id,
      request_id: crypto.randomUUID(),
    })
    const prefix = `/campaign-copies/${created.draft_id}`
    const saved = await call("PATCH", prefix, {
      request_id: crypto.randomUUID(),
      expected_revision: created.revision,
      rows: created.rows.map(
        (row: { row_id: string; configuration: object }) => ({
          row_id: row.row_id,
          configuration: { ...row.configuration, copies: 2 },
        }),
      ),
    })
    const prepared = await call("POST", `${prefix}/preview`, {
      request_id: crypto.randomUUID(),
      expected_revision: saved.revision,
    })
    await new Promise((resolve) => setTimeout(resolve, 1100))
    const ready = await call("GET", prefix, undefined, { bc_id: MAIN_BC_ID })
    const submit = {
      request_id: crypto.randomUUID(),
      expected_revision: ready.revision,
      preview_digest: ready.preview_digest,
    }
    const submitted = await call("POST", `${prefix}/submit`, submit)
    const replayed = await call("POST", `${prefix}/submit`, submit)
    const firstTask = await call(
      "GET",
      `/submissions/${submitted.submission_ids[0]}`,
    )
    return {
      source: source.campaign_id,
      copiedSource: created.rows[0].source.campaign_id,
      children: prepared.children,
      submissionIds: submitted.submission_ids,
      replayIds: replayed.submission_ids,
      firstTask,
    }
  })
  expect(result.copiedSource).toBe(result.source)
  expect(result.children).toHaveLength(2)
  expect(
    new Set(
      result.children.map((child: { preview_id: string }) => child.preview_id),
    ).size,
  ).toBe(2)
  expect(result.submissionIds).toHaveLength(2)
  expect(result.replayIds).toEqual(result.submissionIds)
  expect(result.firstTask).toBeTruthy()
})

test("混合来源定向未修改保存继续继承，更多设置逐组展示实际值", async ({
  page,
}) => {
  const state = await boundary(page, { mixed: true })
  await page.goto(url)
  const row = page.getByTestId("copy-row-row-1")
  await expect(row.getByText("按来源各组定向保留")).toBeVisible()
  await row.getByRole("button", { name: "修改定向", exact: true }).click()
  await expect(
    page.getByText("各广告组定向不同", { exact: false }),
  ).toBeVisible()
  await page.getByRole("button", { name: "保存设置", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "保存草稿", exact: true }),
  ).toBeDisabled()
  await row.getByRole("button", { name: "更多设置", exact: true }).click()
  await expect(page.getByText("美国组", { exact: true })).toBeVisible()
  await expect(page.getByText("英国组", { exact: true })).toBeVisible()
  await expect(
    page.getByText("来源组预算：80 USD · 来源目标 ROAS：1.2", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("来源组预算：120 USD · 来源目标 ROAS：1.5", { exact: true }),
  ).toBeVisible()
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await page.getByRole("button", { name: "生成复制预览", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "创建并启用 2 个新系列", exact: true }),
  ).toBeVisible()
  expect(
    state.mutations.filter((mutation) => mutation.method === "PATCH"),
  ).toHaveLength(0)
  await page.getByRole("button", { name: "核对组设置", exact: true }).click()
  await expect(
    page.getByText("冻结组预算：81 · 冻结目标 ROAS：1.2", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("冻结组预算：121 · 冻结目标 ROAS：1.5", { exact: true }),
  ).toBeVisible()
})

for (const copied of [true, false]) {
  test(`子预览按冻结组显示实际预算定向且普通预览保持原行为 copied=${copied}`, async ({
    page,
  }) => {
    const { buildsBoundary, T, BC, P } = await import("./utils/buildsBoundary")
    const api = await buildsBoundary(page)
    const unified = {
      region_mode: "ALL_AVAILABLE",
      region_codes: [],
      languages: [],
      age_groups: [],
      gender: "GENDER_UNLIMITED",
    }
    Object.assign(api.preview, {
      targeting: unified,
      copy_group_settings: copied
        ? [
            {
              name: "美国复制组",
              budget: "81.00",
              target_roas: "1.2",
              targeting: {
                ...unified,
                region_mode: "SELECTED",
                region_codes: ["US"],
                languages: ["en"],
              },
            },
            {
              name: "英国复制组",
              budget: "121.00",
              target_roas: "1.5",
              targeting: null,
              targeting_summary: "英国 · 英语 · 最低 25 岁",
            },
          ]
        : [],
    })
    await page.route(/\/api\/tenants\/[^/]+\/build-units\/[^/]+$/, (route) =>
      route.fulfill({
        json: {
          bc_id: BC,
          campaign_name: "冻结系列",
          currency: "USD",
          budget: "100.00",
          target_roas: "1.08",
          budget_strategy: "SERIES",
          bid_strategy: "TARGET_ROAS",
          targeting: unified,
          targeting_region_codes: [],
          url: "https://example.com/frozen",
          strategy_version_id: "frozen-strategy",
          scene_snapshot: {},
        },
      }),
    )
    await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
    if (copied) {
      await expect(
        page.getByText("复制冻结配置 · 按广告组核对", { exact: true }),
      ).toBeVisible()
      await expect(
        page.getByText("受众定向 · 严格限制", { exact: true }),
      ).toHaveCount(0)
      await expect(
        page.getByText("冻结组预算：USD 81.00 · 冻结目标 ROAS：1.2", {
          exact: true,
        }),
      ).toBeVisible()
      await expect(
        page.getByText("冻结组预算：USD 121.00 · 冻结目标 ROAS：1.5", {
          exact: true,
        }),
      ).toBeVisible()
      await expect(
        page.getByText("英国 · 英语 · 最低 25 岁", { exact: true }),
      ).toBeVisible()
    } else {
      await expect(
        page.getByText("受众定向 · 严格限制", { exact: true }),
      ).toBeVisible()
      await expect(
        page.getByText("复制冻结配置 · 按广告组核对", { exact: true }),
      ).toHaveCount(0)
    }
    await page.getByRole("tab", { name: "账户组合", exact: true }).click()
    await page
      .getByRole("button", { name: "查看冻结详情", exact: true })
      .first()
      .click()
    const detail = page.getByRole("dialog")
    if (copied) {
      await expect(
        detail.getByText("冻结组预算：USD 81.00 · 冻结目标 ROAS：1.2", {
          exact: true,
        }),
      ).toBeVisible()
      await expect(
        detail.getByText("冻结组预算：USD 121.00 · 冻结目标 ROAS：1.5", {
          exact: true,
        }),
      ).toBeVisible()
      await expect(
        detail.getByText("英国 · 英语 · 最低 25 岁", { exact: true }),
      ).toBeVisible()
      await expect(
        detail.getByText("本账户冻结定向 · 严格限制", { exact: true }),
      ).toHaveCount(0)
      await expect(detail.getByText(/目标 ROAS 1.08/)).toHaveCount(0)
    } else {
      await expect(
        detail.getByText("本账户冻结定向 · 严格限制", { exact: true }),
      ).toBeVisible()
      await expect(detail.getByText(/目标 ROAS 1.08/)).toBeVisible()
      await expect(detail.getByText("美国复制组", { exact: true })).toHaveCount(
        0,
      )
    }
  })
}

test("系列列表勾选后通过批量复制冻结来源并导航到新草稿", async ({ page }) => {
  await boundary(page)
  const selectionId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
  const actions: Array<{
    path: string
    body: Record<string, unknown>
    bcId: string | null
  }> = []
  await page.route("**/api/**", async (route) => {
    const request = route.request()
    const requestUrl = new URL(request.url())
    if (request.method() !== "POST") return route.fallback()
    if (requestUrl.pathname.endsWith("/ad-selections")) {
      actions.push({
        path: "selection",
        body: request.postDataJSON(),
        bcId: requestUrl.searchParams.get("bc_id"),
      })
      return route.fulfill({
        json: {
          selection_id: selectionId,
          snapshot_id: "snapshot-copy-test",
          membership_digest: "c".repeat(64),
          refs: [
            {
              tenant_id: TENANT,
              advertiser_id: "adv-1",
              kind: "campaign",
              remote_id: "campaign-1",
            },
          ],
          material_uses: [],
          expires_at: "2099-01-01T00:00:00Z",
        },
      })
    }
    if (requestUrl.pathname.endsWith("/campaign-copies")) {
      actions.push({
        path: "create",
        body: request.postDataJSON(),
        bcId: requestUrl.searchParams.get("bc_id"),
      })
      return route.fulfill({ status: 201, json: fixture() })
    }
    return route.fallback()
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await page
    .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试", exact: true })
    .check()
  await page.getByRole("button", { name: "批量复制", exact: true }).click()
  await expect(page).toHaveURL(
    new RegExp(`/tenants/${TENANT}/campaign-copies/${DRAFT}`),
  )
  await expect(
    page.getByRole("heading", { name: "批量复制", exact: true }),
  ).toBeVisible()
  expect(actions.map((action) => action.path)).toEqual(["selection", "create"])
  expect(actions[0].bcId).toBe(BC_A)
  expect(actions[0].body).toMatchObject({
    mode: "EXPLICIT",
    row_keys: expect.any(Array),
  })
  expect(actions[0].body.row_keys).toHaveLength(1)
  expect(actions[1].body).toMatchObject({
    bc_id: BC_A,
    selection_id: selectionId,
    request_id: expect.any(String),
  })
  expect(new URL(page.url()).searchParams.get("bc_id")).toBe(BC_A)
})
