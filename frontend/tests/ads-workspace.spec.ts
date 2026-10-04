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
    for (const column of [
      "消耗",
      "广告收益 ROAS",
      "D0 ROAS",
      "广告展示总价值",
      "广告展示事件总数",
      "单次广告展示事件成本",
      "点击率",
    ]) {
      await expect(
        page.getByRole("columnheader", { name: column }),
      ).toBeVisible()
    }
    await expect(
      page.getByRole("columnheader", { name: "来源", exact: true }),
    ).toHaveCount(0)
    await expect(
      page.getByRole("cell", { name: "1.20", exact: true }),
    ).toBeVisible()
    await expect(
      page.getByRole("cell", { name: "外部广告", exact: true }),
    ).toHaveCount(0)
    await expect(
      page.getByRole("cell", { name: "目录未同步", exact: true }),
    ).toHaveCount(0)
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

  test("相同筛选应用也清空选择", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page
      .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" })
      .check()
    await expect(page.getByText("已选 1 条", { exact: true })).toBeVisible()
    await page.getByRole("button", { name: "应用筛选" }).click()
    await expect(page.getByText("已选 1 条", { exact: true })).toHaveCount(0)
  })

  test("从第二页切 BC 不携带旧快照", async ({ page }) => {
    await adsBoundary(page, { paged: true })
    const bcBRequests: string[] = []
    page.on("request", (request) => {
      const url = new URL(request.url())
      if (
        url.pathname.endsWith(`/tenants/${TENANT}/ads`) &&
        url.searchParams.get("bc_id") === "bc-b"
      )
        bcBRequests.push(request.url())
    })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("button", { name: "下一页" }).click()
    await expect(
      page.getByText("嘉书-第二页-测试", { exact: true }),
    ).toBeVisible()
    await page.getByRole("combobox", { name: "当前 BC" }).click()
    await page.getByText("乙 BC", { exact: true }).click()
    await expect(
      page.getByText("乙-其他剧-测试", { exact: true }),
    ).toBeVisible()
    const bcB = new URL(bcBRequests.at(-1) ?? "http://localhost")
    expect(bcB.searchParams.get("cursor")).toBeNull()
    expect(bcB.searchParams.get("snapshot_id")).toBeNull()
  })
})
