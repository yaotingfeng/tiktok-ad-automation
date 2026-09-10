import { expect, test } from "@playwright/test"
import { BC, buildsBoundary, P, T } from "./utils/buildsBoundary"
import { expectWorkspaceLayout } from "./utils/workspaceLayout"

test("冻结预览两剧三账户实际六Campaign，金额使用后端字符串，明细按需读取", async ({
  page,
}) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("6 Campaign · 18 Ad Group · 36 Ad", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("配置日预算合计 USD 600", { exact: true }),
  ).toBeVisible()
  expect(
    api.requests.filter((r) => r.path.includes("/build-units/")),
  ).toHaveLength(0)
  await page.getByRole("tab", { name: "账户组合", exact: true }).click()
  await page.getByRole("button", { name: "查看冻结详情" }).first().click()
  await expect(
    page.getByText("https://example.com/real-frozen-link", { exact: true }),
  ).toBeVisible()
  await page
    .getByText("第 1 组 · 10 份素材 · 2 条 SP 创意", { exact: true })
    .click()
  await expect(
    page.getByText("真实冻结正文 1", { exact: true }).first(),
  ).toBeVisible()
  expect(
    api.requests.filter(
      (r) => r.path.includes("/build-units/") && r.path.endsWith("/groups"),
    ),
  ).toHaveLength(1)
})
test("部分阻断组合和输入问题分别计数，不按已加载页估算", async ({ page }) => {
  await buildsBoundary(page, { blocked: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("4 Campaign · 12 Ad Group · 24 Ad", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("配置日预算合计 USD 400", { exact: true }),
  ).toBeVisible()
  await page.getByRole("tab", { name: "排除组合", exact: true }).click()
  await expect(
    page.getByText("currency_mismatch", { exact: true }),
  ).toHaveCount(2)
  await page.getByRole("tab", { name: "输入问题", exact: true }).click()
  await expect(page.getByText("未解析剧名", { exact: true })).toBeVisible()
})
test("过期预览不可执行创建，保留返回调整", async ({ page }) => {
  await buildsBoundary(page, { previewStatus: "OBSOLETE" })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(page.getByText(/草稿已更新，当前预览已过期/)).toBeVisible()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toBeDisabled()
  await expect(
    page.getByRole("button", { name: "返回调整", exact: true }),
  ).toBeEnabled()
})
for (const width of [1440, 900, 390])
  test(`预览 ${width}px 不产生页面横向溢出`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await buildsBoundary(page)
    await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
    await expect(
      page.getByRole("heading", { name: "搭建预览", exact: true }),
    ).toBeVisible()
    await expectWorkspaceLayout(page)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
  })
test("未知生成预览只查同草稿同revision", async ({ page }) => {
  const { D } = await import("./utils/buildsBoundary"),
    api = await buildsBoundary(page, { previewUnknown: true })
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}`)
  await page.getByRole("button", { name: "生成搭建预览", exact: true }).click()
  await page.getByRole("button", { name: "查询原预览结果" }).click()
  await expect(
    page.getByRole("heading", { name: "搭建预览", exact: true }),
  ).toBeVisible()
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/previews"),
    ),
  ).toHaveLength(1)
  expect(
    api.requests.some(
      (r) => r.method === "GET" && r.path.endsWith("/previews/1"),
    ),
  ).toBe(true)
})

test("默认剧目汇总来自服务端，打开单剧才读取其账户组合", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("tab", { name: "剧目汇总", exact: true }),
  ).toHaveAttribute("data-state", "active")
  expect(api.requests.filter((r) => r.path.endsWith("/units"))).toHaveLength(0)
  await page.getByRole("button", { name: "查看账户组合" }).first().click()
  await expect
    .poll(() => api.requests.filter((r) => r.path.endsWith("/units")).length)
    .toBe(1)
  expect(
    api.requests.find((r) => r.path.endsWith("/units"))!.query.get("drama_id"),
  ).toBeTruthy()
})

for (const width of [1440, 900, 390])
  test(`三步搭建 ${width}px 浏览器验收`, async ({ page }) => {
    const { pickInputs } = await import("./utils/buildsBoundary")
    await page.setViewportSize({ width, height: 900 })
    await buildsBoundary(page)
    await page.goto(`/tenants/${T}/builds/new?bc_id=${BC}`)
    await pickInputs(page)
    await expect(page.locator('[data-slot="dialog-overlay"]')).toHaveCount(0)
    await expectWorkspaceLayout(page)
    await page.evaluate(() => window.scrollTo(0, 0))
    const screenshots = process.env.BUILD_SCREENSHOT_DIR
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    if (screenshots)
      await page.screenshot({
        path: `${screenshots}/task-6-build-input-${width}.png`,
        fullPage: true,
        animations: "disabled",
      })
    await page.getByRole("button", { name: "解析并准备", exact: true }).click()
    await expect(
      page.getByRole("heading", { name: "准备与调整", exact: true }),
    ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "查看与调整素材" }).first(),
    ).toBeVisible()
    await expectWorkspaceLayout(page)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    if (screenshots)
      await page.screenshot({
        path: `${screenshots}/task-6-build-preparation-${width}.png`,
        fullPage: true,
        animations: "disabled",
      })
    await page
      .getByRole("button", { name: "生成搭建预览", exact: true })
      .click()
    await expect(
      page.getByRole("tab", { name: "剧目汇总", exact: true }),
    ).toHaveAttribute("data-state", "active")
    await expect(
      page.getByRole("button", { name: "查看账户组合" }).first(),
    ).toBeVisible()
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    if (screenshots)
      await page.screenshot({
        path: `${screenshots}/task-6-build-preview-${width}.png`,
        fullPage: true,
        animations: "disabled",
      })
  })

for (const suffix of [
  "builds/new",
  `build-drafts/33333333-3333-4333-8333-333333333333`,
  `build-previews/${P}`,
  "build-tasks/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
])
  test(`登录回跳保留搭建URL ${suffix}`, async ({ page }) => {
    await buildsBoundary(page, { loggedOut: true })
    const target = `/tenants/${T}/${suffix}?bc_id=${BC}`
    await page.goto(target)
    await expect(page).toHaveURL(/\/login$/)
    await page.getByLabel("账号", { exact: true }).fill("operator")
    await page.getByLabel("密码", { exact: true }).fill("synthetic-password")
    await page.getByRole("button", { name: "登录", exact: true }).click()
    await expect(page).toHaveURL(target)
  })

test("冻结组合按50/100服务端游标分页，不逐行读取详情", async ({ page }) => {
  const api = await buildsBoundary(page, { unitCount: 131 })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("tab", { name: "账户组合", exact: true }).click()
  await expect(page.getByRole("button", { name: "查看冻结详情" })).toHaveCount(
    50,
  )
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect
    .poll(() => api.requests.filter((r) => r.path.endsWith("/units")).length)
    .toBe(2)
  expect(
    api.requests
      .filter((r) => r.path.endsWith("/units"))[1]
      .query.get("cursor"),
  ).toBe("50")
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(page.getByRole("button", { name: "查看冻结详情" })).toHaveCount(
    100,
  )
  const last = api.requests
    .filter((r) => r.path.endsWith("/units"))
    .slice(-1)[0]
  expect(last.query.get("limit")).toBe("100")
  expect(last.query.get("cursor")).toBeNull()
  expect(
    api.requests.filter((r) => r.path.includes("/build-units/")),
  ).toHaveLength(0)
})

test("明确三级数量后创建并立即启用，仅提交一次并读取真实任务摘要", async ({
  page,
}) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("button", {
      name: "创建并立即启用 6 个 Campaign / 18 个 Ad Group / 36 条 Ad",
      exact: true,
    }),
  ).toBeEnabled()
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await expect(page).toHaveURL(
    /\/build-tasks\/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/,
  )
  await expect(
    page.getByRole("heading", { name: "任务 batch-real", exact: true }),
  ).toBeVisible()
  await expect(page.getByText("排队中", { exact: true })).toBeVisible()
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/submit"),
    ),
  ).toHaveLength(1)
  expect(
    api.requests.some(
      (r) => r.method === "GET" && r.path.includes("/submissions/"),
    ),
  ).toBe(true)
})

test("提交响应丢失后刷新只回查原请求，返回预览不能再次提交", async ({
  page,
}) => {
  const api = await buildsBoundary(page, { submitUnknown: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await expect(
    page.getByRole("button", { name: "查询原提交结果" }),
  ).toBeEnabled()
  const requestId = api.requests.find((r) => r.path.endsWith("/submit"))!.body
    .request_id
  await page.reload()
  await page.getByRole("button", { name: "查询原提交结果" }).click()
  await expect(
    page.getByRole("heading", { name: "任务 batch-real", exact: true }),
  ).toBeVisible()
  expect(
    api.requests.filter(
      (r) =>
        r.method === "GET" &&
        r.path.endsWith(`/submission-requests/${requestId}`),
    ),
  ).toHaveLength(1)
  await page.getByRole("link", { name: "查看冻结预览" }).click()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toBeDisabled()
  await page.getByRole("button", { name: "查看已受理任务" }).click()
  await expect(
    page.getByRole("heading", { name: "任务 batch-real", exact: true }),
  ).toBeVisible()
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/submit"),
    ),
  ).toHaveLength(1)
})

test("原提交404保留同request且切BC先守卫，不把未找到当未创建", async ({
  page,
}) => {
  const api = await buildsBoundary(page, {
    submitUnknown: true,
    lookup404: true,
  })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await page.getByRole("button", { name: "查询原提交结果" }).click()
  await expect(
    page.getByRole("button", { name: "查询原提交结果" }),
  ).toBeEnabled()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toBeDisabled()
  await page.getByRole("combobox", { name: "当前 BC", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "备用 BC" }).click()
  await expect(
    page.getByRole("heading", { name: "提交结果尚待核实" }),
  ).toBeVisible()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(page).toHaveURL(new RegExp(`/build-previews/${P}`))
  await page.getByRole("button", { name: "查询原提交结果" }).click()
  await expect
    .poll(
      () =>
        api.requests.filter((r) => r.path.includes("/submission-requests/"))
          .length,
    )
    .toBe(2)
  const lookups = api.requests.filter((r) =>
    r.path.includes("/submission-requests/"),
  )
  expect(lookups[0].path).toBe(lookups[1].path)
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/submit"),
    ),
  ).toHaveLength(1)
})

test("提交403保留登录并隐藏提交，viewer只读预览", async ({ page }) => {
  const api = await buildsBoundary(page, { submitDenied: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "build-test-token",
  )
  await expect(page.getByText(/你没有执行此操作的权限/).first()).toBeVisible()
  expect(api.requests.filter((r) => r.path.endsWith("/submit"))).toHaveLength(1)
})

test("viewer和零可执行组合不能提交", async ({ page }) => {
  const api = await buildsBoundary(page, { viewer: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("heading", { name: "搭建预览", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
  expect(api.requests.filter((r) => r.path.endsWith("/submit"))).toHaveLength(0)
})

test("零可执行组合保持创建禁用", async ({ page }) => {
  const api = await buildsBoundary(page)
  api.preview.campaign_count = 0
  api.preview.adgroup_count = 0
  api.preview.ad_count = 0
  api.preview.daily_budget_sum = "0.00"
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toBeDisabled()
  expect(api.requests.filter((r) => r.path.endsWith("/submit"))).toHaveLength(0)
})

test("预览后台403保留原提交核实入口并阻止新写入", async ({ page }) => {
  await buildsBoundary(page, { submitUnknown: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await page.getByRole("button", { name: /创建并立即启用/ }).click()
  await expect(
    page.getByRole("button", { name: "查询原提交结果" }),
  ).toBeEnabled()
  await page.route(`**/api/tenants/${T}/build-previews/${P}`, (route) =>
    route.fulfill({ status: 403, json: { code: "action_forbidden" } }),
  )
  await page.getByRole("button", { name: "刷新预览状态" }).click()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: "查询原提交结果" }),
  ).toBeEnabled()
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "build-test-token",
  )
})

test("冻结预算保持长Decimal有效位并只移除小数尾零", async ({ page }) => {
  const api = await buildsBoundary(page)
  api.preview.daily_budget_sum = "9007199254740993123456.123400000000"
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("配置日预算合计 USD 9007199254740993123456.1234", {
      exact: true,
    }),
  ).toBeVisible()
  await page.getByRole("tab", { name: "账户组合", exact: true }).click()
  await expect(page.getByText("USD 100", { exact: true }).first()).toBeVisible()
  await page.getByRole("button", { name: "查看冻结详情" }).first().click()
  await expect(
    page.getByText("Campaign 日预算 USD 100 · ROAS 1.08", { exact: true }),
  ).toBeVisible()
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("BC mismatch preserves tenant identity and directs to the frozen BC", async ({
  page,
}) => {
  const { BC2 } = await import("./utils/buildsBoundary")
  await buildsBoundary(page)
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC2}`)
  await expect(
    page.getByRole("heading", { name: "当前 BC 与预览不一致" }),
  ).toBeVisible()
  await expect(page.getByText("尚未接入租户", { exact: true })).toHaveCount(0)
  await expect(
    page.getByText("尚未连接 TikTok BC", { exact: true }),
  ).toHaveCount(0)
  await page
    .getByRole("link", { name: "切换到资源所属 BC", exact: true })
    .click()
  await expect(page).toHaveURL(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("6 Campaign · 18 Ad Group · 36 Ad", { exact: true }),
  ).toBeVisible()
})
