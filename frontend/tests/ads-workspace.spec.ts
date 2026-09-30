import { expect, test } from "@playwright/test"
import { adsBoundary, BC_A, TENANT } from "./utils/adsBoundary"

test.describe("ads workspace", () => {
  test("外部广告六维切换保留筛选", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("heading", { name: "广告报表" })).toBeVisible()
    await expect(page.getByRole("combobox", { name: "当前 BC" })).toContainText(
      "甲 BC",
    )
    await page.getByRole("textbox", { name: "搜索广告" }).fill("嘉书 总裁归来")
    await page.getByRole("button", { name: "应用筛选" }).click()
    await page.getByRole("tab", { name: "剧" }).click()
    await expect(
      page.getByRole("cell", { name: "总裁归来", exact: true }),
    ).toBeVisible()
    await expect(
      page.getByRole("cell", { name: "1.20", exact: true }),
    ).toBeVisible()
    await expect(
      page.getByRole("cell", { name: "外部广告", exact: true }),
    ).toBeVisible()
  })

  test("切BC忽略迟到响应", async ({ page }) => {
    await adsBoundary(page, { delayedA: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("combobox", { name: "当前 BC" })).toContainText(
      "甲 BC",
    )
    await page.getByRole("combobox", { name: "当前 BC" }).click()
    await page.getByText("乙 BC", { exact: true }).click()
    await expect(
      page.getByText("乙-其他剧-测试", { exact: true }),
    ).toBeVisible()
    await page.waitForTimeout(900)
    await expect(
      page.getByText("嘉书-总裁归来-测试", { exact: true }),
    ).toHaveCount(0)
  })
})
