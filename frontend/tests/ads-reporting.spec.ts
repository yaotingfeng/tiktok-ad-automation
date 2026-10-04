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
  await page.getByRole("button", { name: "嘉书-总裁归来-测试" }).click()
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
  await expect(page.getByText(/报表覆盖不完整/)).toBeVisible()
  await expect(page.getByText("多口径", { exact: true })).toHaveCount(4)
})
