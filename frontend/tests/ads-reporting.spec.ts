import { expect, test } from "@playwright/test"
import { adsBoundary, BC_A, TENANT } from "./utils/adsBoundary"

test("报表状态提示且不再拼接 TikTok 后台链接", async ({ page }) => {
  await adsBoundary(page)
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByRole("heading", { name: "广告报表" })).toBeVisible()
  // 状态收敛成一行结论，不再逐账户铺开，也不使用红色覆盖告警框。
  await expect(page.getByText(/数据更新到 13:00（账户本地）/)).toBeVisible()
  await expect(page.getByText("数据覆盖", { exact: true })).toHaveCount(0)
  await expect(page.getByText(/^\d{19} ·/)).toHaveCount(0)
  await expect(page.getByRole("link", { name: "TikTok 后台" })).toHaveCount(0)
})

test("翻页沿用同一报表快照", async ({ page }) => {
  await adsBoundary(page, { paged: true })
  const requests: string[] = []
  page.on("request", (request) => {
    if (request.url().includes(`/api/tenants/${TENANT}/ads?`))
      requests.push(request.url())
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await expect(page.getByRole("button", { name: "下一页" })).toBeEnabled()
  await page.getByRole("button", { name: "下一页" }).click()
  await expect(
    page.getByText("嘉书-第二页-测试", { exact: true }),
  ).toBeVisible()
  const pageTwo = new URL(requests.at(-1) ?? "http://localhost")
  expect(pageTwo.searchParams.get("cursor")).toBe("cursor-1")
  expect(pageTwo.searchParams.get("snapshot_id")).toBe(
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  )
})

test("详情沿用快照并读取 operation 状态", async ({ page }) => {
  await adsBoundary(page)
  const detailRequests: string[] = []
  page.on("request", (request) => {
    if (request.url().includes("/ads/campaign/campaign-1?"))
      detailRequests.push(request.url())
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  // 点名称打开详情；账户行没有对象引用，因此在系列维度验证。
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await page
    .getByRole("button", { name: "嘉书-总裁归来-测试", exact: true })
    .click()
  await expect(page.getByText("PAUSED", { exact: true })).toBeVisible()
  expect(
    new URL(detailRequests.at(-1) ?? "http://localhost").searchParams.get(
      "snapshot_id",
    ),
  ).toBe("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
})

test("混币种桶不静默合并", async ({ page }) => {
  await adsBoundary(page, { mixedBuckets: true })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  // 汇总卡片保留单行写法；表格单元格按币种逐行显示。
  await expect(
    page.getByText("USD 100.00 / CNY 100.00", { exact: true }),
  ).toHaveCount(1)
  const spendCell = page.locator('tbody td[data-metric="spend"]').first()
  await expect(spendCell.getByText("USD 100.00", { exact: true })).toBeVisible()
  await expect(spendCell.getByText("CNY 100.00", { exact: true })).toBeVisible()
  await expect(page.getByText("多口径", { exact: true })).toHaveCount(0)
})

test("新回收列排序、账户本地更新时间与缺失日期", async ({ page }) => {
  await adsBoundary(page, { incomplete: true })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByText(/数据更新到 13:00（账户本地）/)).toBeVisible()
  await expect(
    page.getByText("1 个账户有日期缺失", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText(/以下日期数据不完整/)).toHaveCount(0)
  await page.getByRole("button", { name: "同步明细", exact: true }).click()
  await expect(page.getByText(/以下日期数据不完整：2026-10-04/)).toBeVisible()
  await page.keyboard.press("Escape")
  const request = page.waitForRequest(
    (r) => new URL(r.url()).searchParams.get("sort_by") === "roas_d30",
  )
  await page
    .getByRole("button", {
      name: "第 29 天广告收入 ROAS，点击排序",
      exact: true,
    })
    .click()
  expect(
    new URL((await request).url()).searchParams.get("sort_direction"),
  ).toBe("desc")
  await page.getByRole("tab", { name: "素材", exact: true }).click()
  // 素材报表不提供的天数列直接隐藏；其余无值统一显示“—”，原因放在悬停说明。
  await expect(page.locator('td[title="平台未提供该指标"]')).toHaveCount(0)
  await expect(
    page.locator('td[title="暂无数据，同步完成后显示"]').first(),
  ).toHaveText("—")
  await expect(
    page.getByRole("cell", { name: "平台未提供", exact: true }),
  ).toHaveCount(0)
})

for (const [status, message] of [
  ["SYNC_DISABLED", "报表同步未开启"],
  ["API_BINDING_REQUIRED", "该 BC 未接入 API，无法同步报表"],
] as const) {
  test(`刷新显示 ${status} 且使用当前筛选账户`, async ({ page }) => {
    await adsBoundary(page, { refreshStatus: status })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(
      page.getByRole("checkbox", { name: "选择 甲账户" }),
    ).toBeVisible()
    const request = page.waitForRequest((r) =>
      r.url().includes("/ad-sync-refresh"),
    )
    await page.getByRole("button", { name: "刷新", exact: true }).click()
    expect((await request).postDataJSON()).toEqual({ advertiser_ids: [] })
    await expect(page.getByText(message, { exact: true })).toBeVisible()
  })
}

test("账户对账提示保留广告汇总", async ({ page }) => {
  await adsBoundary(page, { reconciliation: true })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "账户", exact: true }).click()
  await expect(page.getByLabel("账户报表：USD 101.00")).toBeVisible()
  await expect(page.getByRole("cell", { name: /100.00/ }).first()).toBeVisible()
})

test("同步发布后清除第二页快照和选择并显示放缓", async ({ page }) => {
  await page.clock.install()
  const options = {
    paged: true,
    slowed: true,
    lastSuccess: "2026-10-05T05:00:00Z",
  }
  await adsBoundary(page, options)
  const requests: string[] = []
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.endsWith(`/tenants/${TENANT}/ads`))
      requests.push(request.url())
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await page.getByRole("button", { name: "下一页" }).click()
  await expect(
    page.getByText("嘉书-第二页-测试", { exact: true }),
  ).toBeVisible()
  await page.getByRole("checkbox").last().check()
  await expect(page.getByText("已选 1 条", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "刷新", exact: true }).click()
  await expect(page.getByText("已加入刷新队列")).toBeVisible()
  options.lastSuccess = "2026-10-05T05:01:00Z"
  await page.clock.fastForward(31_000)
  await expect(page.getByText(/数据更新到 13:01/)).toBeVisible()
  await expect(page.getByText("嘉书-第二页-测试", { exact: true })).toHaveCount(
    0,
  )
  await expect(page.getByText("已选 1 条", { exact: true })).toHaveCount(0)
  const latest = new URL(requests.at(-1)!)
  expect(latest.searchParams.get("snapshot_id")).toBeNull()
  expect(latest.searchParams.get("cursor")).toBeNull()
})

test("额度放缓状态可见", async ({ page }) => {
  await adsBoundary(page, { slowed: true })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByText("数据更新放缓", { exact: true })).toBeVisible()
})

test("账户余额单独显示币种和共享资金池", async ({ page }) => {
  await adsBoundary(page, { balance: true })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "账户", exact: true }).click()
  await expect(
    page.getByRole("cell", { name: "CNY 12.50（共享资金池）", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "余额，点击排序", exact: true }),
  ).toBeVisible()
})

test("后台发布不打断第二页选择，用户可选择加载最新数据", async ({ page }) => {
  await page.clock.install()
  const options = { paged: true, lastSuccess: "2026-10-05T05:00:00Z" }
  await adsBoundary(page, options)
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await page.getByRole("button", { name: "下一页" }).click()
  await page.getByRole("checkbox").last().check()
  options.lastSuccess = "2026-10-05T05:01:00Z"
  await page.clock.fastForward(31_000)
  await expect(
    page.getByText("嘉书-第二页-测试", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("已选 1 条", { exact: true })).toBeVisible()
  await page
    .getByRole("button", { name: "有新数据，点击加载", exact: true })
    .click()
  await expect(page.getByText("嘉书-第二页-测试", { exact: true })).toHaveCount(
    0,
  )
  await expect(page.getByText("已选 1 条", { exact: true })).toHaveCount(0)
})
