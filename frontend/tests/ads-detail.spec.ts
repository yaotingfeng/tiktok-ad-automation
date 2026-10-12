import { expect, test } from "@playwright/test"
import { adsBoundary, BC_A, TENANT } from "./utils/adsBoundary"
import { expectLineDrawn } from "./utils/trendChart"

test.describe("ads detail", () => {
  test("点名称打开详情，各页签有内容", async ({ page }) => {
    const trendUrls: string[] = []
    page.on("request", (r) => {
      if (r.url().includes("/ads/trend")) trendUrls.push(r.url())
    })
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告组" }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const sheet = page.getByRole("dialog")
    await expect(sheet.getByRole("tab", { name: "概览" })).toBeVisible()
    await expect(sheet.getByText("版权方")).toBeVisible()
    // 状态枚举显示中文，原值保留在悬停说明里。
    await expect(sheet.getByTitle("APPROVED")).toHaveText("已通过")
    // 页头动作文案与表格工具栏一致。
    const header = sheet.getByTestId("ads-detail-header")
    for (const name of ["启用", "暂停", "修改目标 ROAS", "修改预算"])
      await expect(
        header.getByRole("button", { name, exact: true }),
      ).toBeVisible()
    await sheet.getByRole("tab", { name: "设置" }).click()
    await expect(sheet.getByText("目标 ROAS", { exact: true })).toBeVisible()
    await sheet.getByRole("tab", { name: "趋势" }).click()
    await expect(sheet.getByTestId("ads-trend-chart")).toBeVisible()
    await expectLineDrawn(sheet.getByTestId("ads-trend-chart"))
    // 对象趋势必须同时带账户与对象 ID，覆盖率才按该账户判断。
    const url = new URL(trendUrls.at(-1) ?? "")
    expect(url.searchParams.get("level")).toBe("adgroup")
    expect(url.searchParams.get("advertiser_ids")).toBe("adv-1")
    expect(url.searchParams.get("ids")).toBe("adgroup-1")
    await sheet.getByRole("tab", { name: "操作记录" }).click()
    await expect(sheet.getByText("ENABLE → DISABLE")).toBeVisible()
    // 原值缺失显示为"—"，不显示 null。
    await expect(sheet.getByText("— → 1.2")).toBeVisible()
    // 结果按真实 execution_result 显示中文，被拒绝用醒目样式。
    await expect(sheet.getByText("已执行", { exact: true })).toBeVisible()
    const rejected = sheet.getByText("被拒绝", { exact: true })
    await expect(rejected).toBeVisible()
    await expect(rejected).toHaveClass(/bg-destructive/)
  })

  test("详情内暂停走同一预览流程", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const freeze = page.waitForRequest((r) =>
      r.url().includes("/ad-selections"),
    )
    await page.getByRole("dialog").getByRole("button", { name: "暂停" }).click()
    expect((await freeze).postDataJSON()).toMatchObject({
      mode: "EXPLICIT",
      row_keys: [`${BC_A}-campaign-1`],
    })
    await expect(page.getByRole("heading", { name: "批量暂停" })).toBeVisible()
  })

  test("切 BC 关闭详情", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    await expect(page.getByRole("dialog")).toBeVisible()
    await page.keyboard.press("Escape")
    await page.getByRole("combobox", { name: "当前 BC" }).click()
    await page.getByText("乙 BC", { exact: true }).click()
    await expect(page.getByRole("dialog")).toHaveCount(0)
  })

  test("只读成员详情无操作按钮", async ({ page }) => {
    await adsBoundary(page, { role: "viewer" })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    await expect(
      page.getByRole("dialog").getByRole("tab", { name: "概览" }),
    ).toBeVisible()
    await expect(
      page.getByRole("dialog").getByRole("button", { name: "暂停" }),
    ).toHaveCount(0)
  })

  test("账户详情显示余额与时区，不请求对象详情接口", async ({ page }) => {
    const detailCalls: string[] = []
    page.on("request", (r) => {
      if (/\/ads\/(campaign|adgroup|ad|creative)\//.test(r.url()))
        detailCalls.push(r.url())
    })
    await adsBoundary(page, { balance: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("button", { name: "甲账户", exact: true }).click()
    const sheet = page.getByRole("dialog")
    await expect(sheet.getByText("余额")).toBeVisible()
    await expect(sheet.getByText("12.50")).toBeVisible()
    await expect(sheet.getByText("时区")).toBeVisible()
    await expect(sheet.getByRole("tab", { name: "设置" })).toHaveCount(0)
    expect(detailCalls).toHaveLength(0)
  })

  test("剧详情列出关联系列并可切换到系列详情", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "剧" }).click()
    await page.getByRole("button", { name: "总裁归来", exact: true }).click()
    const sheet = page.getByRole("dialog")
    await expect(sheet.getByText("关联系列")).toBeVisible()
    const detail = page.waitForRequest((r) =>
      r.url().includes("/ads/campaign/campaign-1"),
    )
    await sheet.getByRole("button", { name: "campaign-1" }).click()
    await detail
    await expect(sheet.getByRole("tab", { name: "设置" })).toBeVisible()
  })

  for (const [timezone, zone, first] of [
    ["Asia/Hong_Kong", "Asia/Hong_Kong", "09:00"],
    ["MIXED", "UTC", "01:00"],
  ]) {
    test(`小时趋势按账户时区显示（${timezone}）`, async ({ page }) => {
      await adsBoundary(page, { trendTimezone: timezone })
      await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
      await page.getByRole("tab", { name: "系列" }).click()
      await page
        .getByRole("button", { name: /嘉书-总裁归来/ })
        .first()
        .click()
      const sheet = page.getByRole("dialog")
      await sheet.getByRole("tab", { name: "趋势" }).click()
      const hourly = page.waitForRequest((r) =>
        r.url().includes("granularity=hour"),
      )
      await sheet.getByRole("button", { name: "按小时" }).click()
      await hourly
      await expect(
        sheet.getByRole("columnheader", { name: `时间（${zone}）` }),
      ).toBeVisible()
      await expect(
        sheet.getByRole("cell", { name: first, exact: true }),
      ).toBeVisible()
    })
  }
})
