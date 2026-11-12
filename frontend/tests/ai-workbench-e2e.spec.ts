import { expect, test } from "@playwright/test"

const tenant = "11111111-1111-4111-8111-111111111111"
const optimizer = "22222222-2222-4222-8222-222222222222"
const sessionTenants = [
  tenant,
  "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
]

// 登录态与租户列表的 transport double；用例里后注册的路由优先于这里的兜底。
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() =>
    localStorage.setItem("access_token", "ai-workbench-test-token"),
  )
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === "/api/users/me")
      return route.fulfill({
        json: {
          id: "44444444-4444-4444-8444-444444444444",
          username: "ai-test",
          full_name: "AI 测试",
          is_active: true,
          is_superuser: false,
        },
      })
    if (path === "/api/me/tenants")
      return route.fulfill({
        json: {
          items: sessionTenants.map((id, index) => ({
            id,
            name: `租户 ${index + 1}`,
            active: true,
            role: "tenant_admin",
            default_bc_id: "bc-a",
          })),
          next_cursor: null,
          total: sessionTenants.length,
        },
      })
    return route.fulfill({ json: { items: [], next_cursor: null, total: 0 } })
  })
})

test("AI 每日报告使用 transport double 展示冻结证据和待确认状态", async ({
  page,
}) => {
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: optimizer,
            name: "自动优化师",
            active: true,
            version: 2,
            bc_id: "bc-a",
            advertiser_ids: ["acc-1"],
            strategy_version_id: "strategy-2",
            optimization_mode: "AUTO",
            rule_ids: [],
          },
        ],
      },
    }),
  )
  await page.route(
    `**/api/tenants/${tenant}/ai-optimizers/${optimizer}/daily-report**`,
    (route) =>
      route.fulfill({
        json: {
          business_date: "2026-11-12",
          optimization_mode: "AUTO",
          target_progress: {
            planned: 1,
            completed: 1,
            pending: 0,
            failed: 0,
            unknown: 0,
          },
          schedule_rows: [
            {
              id: "plan-1",
              title: "剧集 A",
              status: "COMPLETE",
              order_materials: true,
            },
          ],
          material: { requested: 1, not_requested: 0, pending: 0, matched: 1 },
          rule_actions: [],
          pending_proposals: [{ id: "proposal-1", status: "PENDING" }],
          frozen_evidence_ids: ["evidence-1"],
          next_trigger: null,
        },
      }),
  )
  await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}`)
  await expect(page.getByTestId("ai-daily-report")).toContainText("每日报告")
  await expect(page.getByTestId("ai-daily-report")).toContainText("剧集 A")
  await expect(page.getByTestId("ai-daily-report")).toContainText("待确认 1 条")
})

test("切换优化师后每日报告请求保持租户边界", async ({ page }) => {
  const requested: string[] = []
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: optimizer,
            name: "手动优化师",
            active: true,
            version: 1,
            bc_id: "bc-a",
            advertiser_ids: ["acc-1"],
            strategy_version_id: "strategy-1",
            optimization_mode: "MANUAL",
            rule_ids: [],
          },
        ],
      },
    }),
  )
  await page.route(
    "**/api/tenants/*/ai-optimizers/*/daily-report**",
    async (route) => {
      requested.push(route.request().url())
      await route.fulfill({
        json: {
          target_progress: {},
          schedule_rows: [],
          material: {},
          rule_actions: [],
          pending_proposals: [],
          frozen_evidence_ids: [],
        },
      })
    },
  )
  await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}`)
  await expect.poll(() => requested.length).toBeGreaterThan(0)
  expect(
    requested.every((url) => url.includes(`/api/tenants/${tenant}/`)),
  ).toBeTruthy()
})

test("双租户双优化师切换保留 AUTO/MANUAL、下单路径和回读边界", async ({
  page,
}) => {
  const tenantA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  const tenantB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
  const optimizerA = "aaaaaaaa-0000-4000-8000-aaaaaaaaaaaa"
  const optimizerAManual = "aaaaaaaa-0001-4000-8000-aaaaaaaaaaaa"
  const optimizerB = "bbbbbbbb-0000-4000-8000-bbbbbbbbbbbb"
  const reportRequests: string[] = []
  const proposalRequests: string[] = []
  const versionRequests: Array<Record<string, unknown>> = []
  const optimizerPayload = (
    id: string,
    mode: "AUTO" | "MANUAL",
    name: string,
  ) => ({
    id,
    name,
    active: true,
    version: mode === "AUTO" ? 2 : 3,
    bc_id: id.startsWith("bbbb") ? "bc-b" : "bc-a",
    advertiser_ids: [id.startsWith("bbbb") ? "acc-b" : "acc-a"],
    strategy_version_id: `strategy-${id}`,
    optimization_mode: mode,
    rule_ids: [id.startsWith("bbbb") ? "rule-b-account" : "rule-a-account"],
  })
  await page.route("**/api/tenants/*/ai-optimizers?**", async (route) => {
    const url = route.request().url()
    if (url.includes(`/tenants/${tenantA}/`)) {
      await route.fulfill({
        json: {
          items: [
            optimizerPayload(optimizerA, "AUTO", "自动优化师 A"),
            optimizerPayload(optimizerAManual, "MANUAL", "手动优化师 A"),
          ],
        },
      })
      return
    }
    if (url.includes(`/tenants/${tenantB}/`)) {
      await route.fulfill({
        json: {
          items: [optimizerPayload(optimizerB, "MANUAL", "手动优化师 B")],
        },
      })
      return
    }
    await route.fulfill({
      status: 404,
      json: { detail: "cross-tenant optimizer lookup" },
    })
  })
  await page.route(
    "**/api/tenants/*/ai-optimizers/*/daily-report**",
    async (route) => {
      const url = route.request().url()
      reportRequests.push(url)
      const tenantMatch = url.match(
        /\/api\/tenants\/([^/]+)\/ai-optimizers\/([^/]+)\//,
      )
      const reportTenant = tenantMatch?.[1]
      const reportOptimizer = tenantMatch?.[2]
      const valid =
        (reportTenant === tenantA &&
          [optimizerA, optimizerAManual].includes(reportOptimizer ?? "")) ||
        (reportTenant === tenantB && reportOptimizer === optimizerB)
      if (!valid) {
        await route.fulfill({
          status: 404,
          json: { detail: "cross-tenant report lookup" },
        })
        return
      }
      await route.fulfill({
        json: {
          business_date: "2026-11-12",
          optimization_mode: reportOptimizer === optimizerA ? "AUTO" : "MANUAL",
          target_progress: {
            planned: 2,
            completed: 1,
            partial: 1,
            pending: 0,
            failed: 0,
            unknown: 0,
          },
          schedule_rows: [
            {
              id: `${reportOptimizer}-ordered`,
              title: "需下单剧",
              status: "PARTIAL",
              order_materials: true,
            },
            {
              id: `${reportOptimizer}-existing`,
              title: "已有素材剧",
              status: "COMPLETE",
              order_materials: false,
            },
          ],
          build_outcomes: [
            {
              id: `${reportOptimizer}-build`,
              status: "PARTIAL",
              submission_id: `${reportOptimizer}-submission`,
              readback: { matched: 1, source: "tkada" },
            },
          ],
          material: {
            requested: 1,
            not_requested: 1,
            pending: 0,
            matched: 1,
            unknown: 0,
          },
          rule_actions: [
            {
              rule_id:
                reportOptimizer === optimizerB
                  ? "rule-b-account"
                  : "rule-a-account",
            },
          ],
          pending_proposals: [
            { id: `${reportOptimizer}-proposal`, status: "PENDING" },
          ],
          frozen_evidence_ids: [`${reportOptimizer}-evidence`],
          next_trigger: null,
        },
      })
    },
  )
  await page.route(
    `**/api/tenants/${tenantA}/ai-optimizers/${optimizerAManual}`,
    async (route) => {
      if (route.request().method() !== "PATCH") return route.continue()
      const body = route.request().postDataJSON() as Record<string, unknown>
      versionRequests.push(body)
      await route.fulfill({
        json: {
          ...optimizerPayload(optimizerAManual, "MANUAL", "手动优化师 A"),
          version: 4,
          request_id: body.request_id,
        },
      })
    },
  )
  await page.route(
    `**/api/tenants/${tenantA}/ai-optimizers/${optimizerAManual}/proposals**`,
    async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({
          json: [
            {
              id: `${optimizerAManual}-proposal`,
              status: "PENDING",
              title: "复盘建议",
            },
          ],
        })
        return
      }
      proposalRequests.push(route.request().url())
      await route.fulfill({
        json: {
          id: `${optimizerAManual}-proposal`,
          status: "ACCEPTED",
          published_version_id: "strategy-next",
        },
      })
    },
  )

  await page.goto(`/tenants/${tenantA}/ai-workbench?optimizer_id=${optimizerA}`)
  await expect(page.getByTestId("ai-daily-report")).toContainText("AUTO")
  await expect(page.getByTestId("ai-daily-report")).toContainText("部分完成")
  await expect(page.getByTestId("ai-daily-report")).toContainText("需下单剧")
  await page.goto(
    `/tenants/${tenantA}/ai-workbench?optimizer_id=${optimizerAManual}`,
  )
  await expect(page.getByTestId("ai-daily-report")).toContainText("MANUAL")
  await expect(page.getByTestId("ai-daily-report")).toContainText("部分完成")
  await page.goto(
    `/tenants/${tenantA}/ai-workbench?optimizer_id=${optimizerAManual}&view=pending`,
  )
  await expect(page.getByTestId("ai-pending-view")).toContainText("复盘建议")
  await page.getByRole("button", { name: "接受" }).click()
  await expect.poll(() => proposalRequests.length).toBe(1)
  expect(proposalRequests[0]).toContain(
    `/proposals/${optimizerAManual}-proposal/accept`,
  )

  await page.goto(`/tenants/${tenantB}/ai-workbench?optimizer_id=${optimizerB}`)
  // 优化师名称显示在左侧列表；报告卡片只承载模式与冻结证据。
  await expect(page.getByTestId("ai-workbench-shell")).toContainText(
    "手动优化师 B",
  )
  await expect(page.getByTestId("ai-daily-report")).toContainText("MANUAL")
  await expect.poll(() => reportRequests.length).toBeGreaterThanOrEqual(3)
  expect(
    reportRequests.every(
      (url) =>
        url.includes("/api/tenants/") &&
        !url.includes(`/tenants/${tenantA}/ai-optimizers/${optimizerB}/`),
    ),
  ).toBeTruthy()
  expect(
    reportRequests.some((url) =>
      url.includes(`/tenants/${tenantB}/ai-optimizers/${optimizerB}/`),
    ),
  ).toBeTruthy()
})

const configuredOptimizer = (
  id = optimizer,
  name = "优化师 A",
  active = true,
) => ({
  id,
  name,
  active,
  version: 1,
  bc_id: "bc-a",
  advertiser_ids: ["acc-1"],
  strategy_version_id: "version-1",
  optimization_mode: "MANUAL",
  daily_spend: "100",
  target_roas: "1.2",
  priority: "BALANCED",
  rule_ids: ["rule-1"],
  timezone: "Asia/Shanghai",
  review_time: "13:00",
  authorization_limits: {},
})

async function choose(
  page: import("@playwright/test").Page,
  label: string,
  name: string,
) {
  await page.getByRole("combobox", { name: label, exact: true }).click()
  await page.getByRole("option", { name, exact: true }).click()
}

test("空租户可创建、编辑、暂停和恢复优化师，所有业务请求带 Bearer", async ({
  page,
}) => {
  let items: ReturnType<typeof configuredOptimizer>[] = []
  const writes: Array<Record<string, unknown>> = []
  const authenticated: string[] = []
  await page.route(`**/api/tenants/${tenant}/**`, async (route) => {
    const request = route.request()
    expect(request.headers().authorization).toBe(
      "Bearer ai-workbench-test-token",
    )
    authenticated.push(new URL(request.url()).pathname)
    const path = new URL(request.url()).pathname
    const method = request.method()
    if (path.endsWith("/bcs"))
      return route.fulfill({
        json: { items: [{ bc_id: "bc-a", name: "星屿" }], next_cursor: null },
      })
    if (path.endsWith("/accounts"))
      return route.fulfill({
        json: {
          items: [
            {
              advertiser_id: "acc-1",
              bc_id: "bc-a",
              name: "账户一",
              can_build: true,
            },
          ],
          next_cursor: null,
        },
      })
    if (path.endsWith("/strategies"))
      return route.fulfill({
        json: {
          items: [
            {
              id: "strategy-1",
              name: "初始策略",
              version_id: "version-1",
              latest_version: 1,
              active: true,
            },
            {
              id: "strategy-2",
              name: "新策略",
              version_id: "version-2",
              latest_version: 2,
              active: true,
            },
          ],
          next_cursor: null,
        },
      })
    if (path.endsWith("/automation-rules"))
      return route.fulfill({
        json: { items: [{ id: "rule-1", name: "控损规则" }] },
      })
    if (path.endsWith("/ai-optimizers") && method === "GET")
      return route.fulfill({ json: { items, next_cursor: null } })
    if (
      (path.endsWith("/ai-optimizers") && method === "POST") ||
      method === "PATCH"
    ) {
      const body = request.postDataJSON()
      writes.push(body)
      items = [
        {
          ...(items[0] ?? configuredOptimizer()),
          ...body,
          version: writes.length,
        },
      ]
      return route.fulfill({ json: items[0] })
    }
    if (path.endsWith("/daily-report"))
      return route.fulfill({
        json: { target_progress: {}, material: {}, schedule_rows: [] },
      })
    return route.fulfill({ json: { items: [], total: 0 } })
  })
  await page.goto(`/tenants/${tenant}/ai-workbench`)
  await page.getByRole("button", { name: "创建优化师", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByLabel("优化师名称").fill("第一位优化师")
  await choose(page, "BC", "星屿 · bc-a")
  await dialog.getByLabel("账户一 · acc-1").check()
  await dialog.getByLabel("日消耗目标").fill("150")
  await dialog.getByLabel("目标 ROAS").fill("1.3")
  await choose(page, "绑定策略版本", "初始策略 · v1")
  await dialog.getByLabel("控损规则").check()
  await dialog.getByLabel("允许调整绑定策略").check()
  await dialog.getByLabel("单次预算调整绝对上限").fill("20")
  await dialog.getByLabel("素材制作策略 ID").fill("cstrategy-1,cstrategy-2")
  await dialog.getByLabel("素材回写表格").fill("sheet-1")
  await dialog.getByRole("button", { name: "创建优化师", exact: true }).click()
  await expect(dialog).not.toBeVisible()
  expect(writes[0]).toMatchObject({
    name: "第一位优化师",
    advertiser_ids: ["acc-1"],
    strategy_version_id: "version-1",
    rule_ids: ["rule-1"],
    timezone: "Asia/Shanghai",
    review_time: "13:00",
    authorization_limits: { strategy: true, max_budget_delta: 20 },
    material_config: {
      strategy_ids: ["cstrategy-1", "cstrategy-2"],
      feishu_token: "sheet-1",
    },
  })
  await page.getByRole("button", { name: "编辑优化师" }).click()
  await dialog.getByLabel("优化师名称").fill("已编辑优化师")
  await choose(page, "绑定策略版本", "新策略 · v2")
  await choose(page, "优化模式", "自动优化")
  await dialog.getByLabel("每日复盘时间").fill("15:30")
  await dialog.getByRole("button", { name: "保存优化师" }).click()
  await expect(dialog).not.toBeVisible()
  expect(writes[1]).toMatchObject({
    name: "已编辑优化师",
    strategy_version_id: "version-2",
    optimization_mode: "AUTO",
    review_time: "15:30",
  })
  await page.getByRole("button", { name: "暂停优化师" }).click()
  await expect(page.getByRole("button", { name: "恢复优化师" })).toBeVisible()
  await expect(page.getByTestId("ai-optimizer-select")).toHaveValue(optimizer)
  expect(writes[2]).toMatchObject({ active: false })
  await page.getByRole("button", { name: "恢复优化师" }).click()
  await expect(page.getByRole("button", { name: "暂停优化师" })).toBeVisible()
  expect(writes[3]).toMatchObject({ active: true })
  expect(authenticated.some((path) => path.endsWith("/accounts"))).toBeTruthy()
})

test("提案展示前后值与授权证据，决定错误可见，复盘可触发并回读状态", async ({
  page,
}) => {
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({ json: { items: [configuredOptimizer()] } }),
  )
  const proposal = {
    id: "proposal-1",
    kind: "strategy",
    status: "PENDING",
    before: { budget: 100 },
    after: { budget: 120 },
    evidence: { evidence_id: "frozen-123" },
    authorization: { advertiser_ids: ["acc-1"], within_scope: true },
    base_optimizer_version: 1,
  }
  await page.route(
    `**/ai-optimizers/${optimizer}/proposals**`,
    async (route) => {
      expect(route.request().headers().authorization).toBe(
        "Bearer ai-workbench-test-token",
      )
      if (route.request().method() === "POST")
        return route.fulfill({
          status: 409,
          json: { detail: "版本已变更，请重新复盘" },
        })
      return route.fulfill({ json: [proposal] })
    },
  )
  let reviews = 0
  await page.route(`**/ai-optimizers/${optimizer}/reviews`, async (route) => {
    expect(route.request().headers().authorization).toBe(
      "Bearer ai-workbench-test-token",
    )
    if (route.request().method() === "GET")
      return route.fulfill({ json: { runtime_ready: true, items: [] } })
    expect(route.request().postDataJSON().request_id).toBeTruthy()
    reviews += 1
    return route.fulfill({
      json: { task: { id: "review-1", status: "QUEUED" }, proposals: [] },
    })
  })
  await page.route(`**/ai-optimizers/${optimizer}/reviews/review-1`, (route) =>
    route.fulfill({ json: { id: "review-1", status: "COMPLETE" } }),
  )
  await page.goto(
    `/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}&view=review`,
  )
  await expect(page.getByTestId("ai-review-view")).toContainText("frozen-123")
  await expect(page.getByTestId("ai-review-view")).toContainText(
    '"budget": 120',
  )
  await expect(page.getByTestId("ai-review-view")).toContainText(
    "授权与影响范围",
  )
  await page.getByRole("button", { name: "接受", exact: true }).click()
  await expect(page.getByRole("alert")).toContainText("版本已变更")
  await page.getByRole("button", { name: "发起复盘" }).click()
  await expect(page.getByRole("status")).toContainText("COMPLETE")
  expect(reviews).toBe(1)
})

test("切换优化师丢弃旧提案和编辑草稿，已暂停优化师仍可管理", async ({
  page,
}) => {
  const second = "33333333-3333-4333-8333-333333333333"
  let releaseOld: (() => void) | undefined
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({
      json: {
        items: [
          configuredOptimizer(),
          configuredOptimizer(second, "暂停优化师 B", false),
        ],
      },
    }),
  )
  await page.route(
    `**/ai-optimizers/${optimizer}/proposals**`,
    async (route) => {
      await new Promise<void>((resolve) => {
        releaseOld = resolve
      })
      await route
        .fulfill({
          json: [{ id: "old", status: "PENDING", title: "旧优化师建议" }],
        })
        .catch(() => {})
    },
  )
  await page.route(`**/ai-optimizers/${second}/proposals**`, (route) =>
    route.fulfill({
      json: [{ id: "new", status: "REJECTED", title: "新优化师历史" }],
    }),
  )
  await page.goto(
    `/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}&view=review`,
  )
  await expect.poll(() => !!releaseOld).toBeTruthy()
  await page.getByTestId("ai-optimizer-select").selectOption(second)
  await expect(page.getByTestId("ai-review-view")).toContainText("新优化师历史")
  releaseOld?.()
  await expect(page.getByTestId("ai-review-view")).not.toContainText(
    "旧优化师建议",
  )
  await expect(
    page.getByRole("button", { name: "接受", exact: true }),
  ).toHaveCount(0)
  await page.getByTestId("ai-nav-overview").click()
  await expect(page.getByRole("button", { name: "恢复优化师" })).toBeVisible()
})

test("排期保存保留行 ID、冻结行只读，输入不中断，TSV 和日期切换可用", async ({
  page,
}) => {
  // 此场景在次日切到空排期；固定起始日，避免真实日期变化直接命中空数据分支。
  await page.clock.setFixedTime(new Date("2026-11-12T04:00:00Z"))
  const value = configuredOptimizer()
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({ json: { items: [value] } }),
  )
  await page.route(
    `**/api/tenants/${tenant}/ai-optimizers/${optimizer}`,
    (route) => route.fulfill({ json: value }),
  )
  const saved: Array<Record<string, unknown>> = []
  await page.route(`**/ai-optimizers/${optimizer}/daily-plans**`, (route) => {
    const request = route.request()
    expect(request.headers().authorization).toBe(
      "Bearer ai-workbench-test-token",
    )
    const date = new URL(request.url()).searchParams.get("business_date")
    const row = {
      id: "row-frozen",
      plan_id: "plan-1",
      position: 0,
      read_only: true,
      business_date: date,
      optimizer_id: optimizer,
      title: "已执行剧",
      scheduled_time: "09:00",
      provider_connection_id: "provider-1",
      application_id: "app-1",
      external_drama_id: "0012",
      promotion_url: "",
      order_materials: true,
    }
    if (request.method() === "GET")
      return route.fulfill({
        json: { revision: 1, rows: date === "2026-11-13" ? [] : [row] },
      })
    const body = request.postDataJSON()
    saved.push(body)
    return route.fulfill({
      json: {
        revision: 2,
        rows: body.rows.map(
          (entry: Record<string, unknown>, index: number) => ({
            ...entry,
            id: entry.id ?? `new-${index}`,
            read_only: index === 0,
          }),
        ),
      },
    })
  })
  await page.goto(
    `/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}&view=schedule`,
  )
  await expect(page.getByTestId("plan-cell-0-1")).toHaveValue("已执行剧")
  await expect(page.getByTestId("plan-cell-0-1")).toBeDisabled()
  await expect(
    page.getByTestId("drama-plan-table").locator("tbody tr"),
  ).toHaveCount(10)
  const input = page.getByTestId("plan-cell-1-1")
  await input.pressSequentially("New drama")
  await expect(input).toHaveValue("New drama")
  await expect(input).toBeFocused()
  await page.getByTestId("plan-cell-1-0").evaluate((element) => {
    const data = new DataTransfer()
    data.setData("text/plain", "10:00\t新剧\tprovider-1\tapp-1\t\t0013\t否")
    element.dispatchEvent(
      new ClipboardEvent("paste", { clipboardData: data, bubbles: true }),
    )
  })
  await expect(input).toHaveValue("新剧")
  await page.getByRole("button", { name: "撤销" }).click()
  await expect(input).toHaveValue("New drama")
  await input.press("Tab")
  await expect(page.getByTestId("plan-cell-1-2")).toBeFocused()
  await input.press("Enter")
  await expect(page.getByTestId("plan-cell-2-1")).toBeFocused()
  await page.getByRole("button", { name: "保存剧单" }).click()
  await expect.poll(() => saved.length).toBe(1)
  const rows = saved[0].rows as Array<Record<string, unknown>>
  expect(rows[0].id).toBe("row-frozen")
  expect(
    rows.every(
      (row) =>
        !("read_only" in row) && !("plan_id" in row) && !("position" in row),
    ),
  ).toBeTruthy()
  await page.getByTestId("plan-date").fill("2026-11-13")
  await expect(page.getByTestId("plan-cell-0-1")).toHaveValue("")
  await expect(page.getByRole("button", { name: "撤销" })).toBeDisabled()
})

for (const width of [390, 1024, 1440]) {
  test(`工作台在 ${width}px 保持容器宽度`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
      route.fulfill({ json: { items: [configuredOptimizer()] } }),
    )
    await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}`)
    await expect(page.getByTestId("ai-optimizer-config")).toBeVisible()
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    )
    expect(overflow).toBe(false)
  })
}

test("复盘历史支持失败重试、取消排队任务和服务未配置提示", async ({ page }) => {
  await page.route(`**/api/tenants/${tenant}/ai-optimizers?**`, (route) =>
    route.fulfill({ json: { items: [configuredOptimizer()] } }),
  )
  await page.route(`**/ai-optimizers/${optimizer}/proposals**`, (route) =>
    route.fulfill({ json: [] }),
  )
  let status = "FAILED"
  const actions: string[] = []
  await page.route(`**/ai-optimizers/${optimizer}/reviews**`, async (route) => {
    const path = new URL(route.request().url()).pathname
    expect(route.request().headers().authorization).toBe(
      "Bearer ai-workbench-test-token",
    )
    if (path.endsWith("/reviews"))
      return route.fulfill({
        json: {
          runtime_ready: true,
          items: [
            {
              id: "historical-task",
              status,
              result: { summary: "上次服务暂时不可用" },
            },
          ],
        },
      })
    if (route.request().method() === "POST") {
      expect(route.request().postDataJSON()).toEqual({})
      actions.push(path.split("/").at(-1)!)
      status = path.endsWith("/retry") ? "PENDING" : "CANCELLED"
    }
    return route.fulfill({ json: { id: "historical-task", status } })
  })
  await page.goto(
    `/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}&view=review`,
  )
  await expect(page.getByRole("status")).toContainText("上次服务暂时不可用")
  await page.getByRole("button", { name: "重试复盘", exact: true }).click()
  await page.getByRole("button", { name: "取消复盘", exact: true }).click()
  await expect(page.getByRole("status")).toContainText("CANCELLED")
  expect(actions).toEqual(["retry", "cancel"])
  await page.route(`**/ai-optimizers/${optimizer}/reviews`, (route) =>
    route.fulfill({ json: { runtime_ready: false, items: [] } }),
  )
  await page.reload()
  await expect(
    page.getByText("复盘服务尚未配置，暂不能发起复盘。"),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "发起复盘", exact: true }),
  ).toBeDisabled()
})
