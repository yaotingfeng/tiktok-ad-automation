import { expect, test } from "@playwright/test"
import { adsBoundary, BC_A, TENANT } from "./utils/adsBoundary"

test("报表状态提示与官方链接门禁", async ({ page }) => {
  await adsBoundary(page)
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByRole("heading", { name: "广告报表" })).toBeVisible()
  await expect(page.getByText("数据覆盖", { exact: true })).toBeVisible()
  await expect(page.getByRole("link", { name: "TikTok 后台" })).toHaveAttribute(
    "href",
    /https:\/\/ads\.tiktok\.com\/i18n\//,
  )
})

test("翻页沿用同一报表快照", async ({ page }) => {
  await adsBoundary(page, { paged: true })
  const requests: string[] = []
  page.on("request", (request) => {
    if (request.url().includes(`/api/tenants/${TENANT}/ads?`))
      requests.push(request.url())
  })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
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
  await page.getByRole("button", { name: "查看详情" }).click()
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
  await expect(
    page.getByText("USD 100.00 / CNY 100.00", { exact: true }),
  ).toHaveCount(2)
  await expect(page.getByText("多口径", { exact: true })).toHaveCount(0)
})

test("新回收列排序、账户本地更新时间与缺失日期", async ({ page }) => {
  await adsBoundary(page, { incomplete: true })
  await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
  await expect(page.getByText(/数据更新到 13:00（账户本地）/)).toBeVisible()
  await expect(page.getByText(/以下日期数据不完整：2026-10-04/)).toBeVisible()
  const request = page.waitForRequest(
    (r) => new URL(r.url()).searchParams.get("sort_by") === "roas_d30",
  )
  await page
    .getByRole("button", { name: "D30 ROAS，点击排序", exact: true })
    .click()
  expect(
    new URL((await request).url()).searchParams.get("sort_direction"),
  ).toBe("desc")
  await page.getByRole("tab", { name: "素材", exact: true }).click()
  await expect(
    page.getByRole("cell", { name: "平台未提供", exact: true }),
  ).toHaveCount(2)
})

for (const [status, message] of [
  ["SYNC_DISABLED", "报表同步未开启"],
  ["API_BINDING_REQUIRED", "该 BC 未接入 API，无法同步报表"],
] as const) {
  test(`刷新显示 ${status} 且使用当前筛选账户`, async ({ page }) => {
    await adsBoundary(page, { refreshStatus: status })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("button", { name: "查看详情" })).toBeVisible()
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
