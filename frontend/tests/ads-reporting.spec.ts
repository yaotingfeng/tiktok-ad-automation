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
