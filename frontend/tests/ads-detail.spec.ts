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
    // 素材挂在广告下面，广告组详情不显示素材页签。
    await expect(sheet.getByRole("tab", { name: "素材" })).toHaveCount(0)
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

  test("广告详情显示本广告素材数据，只启停本广告的素材", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告", exact: true }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const sheet = page.getByRole("dialog")
    await sheet.getByRole("tab", { name: "素材" }).click()
    await expect(sheet.getByText("ep1.mp4")).toBeVisible()
    await expect(sheet.getByText("12.50")).toBeVisible()
    await sheet.getByRole("checkbox", { name: "选择 ep2.mp4" }).click()
    const freeze = page.waitForRequest((r) =>
      r.url().includes("/ads/ad/ad-1/material-selections"),
    )
    await sheet
      .getByRole("button", { name: "暂停", exact: true })
      .last()
      .click()
    const request = await freeze
    expect(request.postDataJSON()).toEqual({ ad_material_ids: ["m2"] })
    expect(new URL(request.url()).searchParams.get("advertiser_id")).toBe(
      "adv-1",
    )
    await expect(page.getByRole("heading", { name: "批量暂停" })).toBeVisible()
  })

  for (const role of ["operator", "viewer"] as const) {
    test(`广告详情素材指标支持数值排序（${role}）`, async ({ page }) => {
      await adsBoundary(page, { role })
      const metrics = [
        ["spend", "消耗"],
        ["impressions", "展示量"],
        ["clicks", "点击量（目标页面）"],
        ["revenue_d0", "第 0 天 广告收益金额"],
        ["roas_d0", "第 0 天广告收益 ROAS"],
        ["revenue_total", "广告展示总价值"],
        ["roas_total", "广告收益 ROAS"],
      ]
      // 含小数、零、同值、NULL、空串、缺失字段和无效值，避免按显示文本排序。
      const values = ["3", "12.5", "0", null, "12.5", "", undefined, "NaN"]
      await page.route("**/ads/ad/ad-1/materials?*", (route) =>
        route.fulfill({
          json: {
            coverage: { status: "COMPLETE" },
            items: values.map((_, index) => ({
              use_ref: {
                ad_ref: {
                  tenant_id: TENANT,
                  advertiser_id: "adv-1",
                  kind: "ad",
                  remote_id: "ad-1",
                },
                platform_material_id: `video-m${index + 1}`,
                ad_material_id: `m${index + 1}`,
                material_type: "VIDEO",
              },
              name: `ep${index + 1}.mp4`,
              operation_status: "ENABLE",
              metric_buckets: [
                {
                  currency: "USD",
                  timezone: "UTC",
                  values: Object.fromEntries(
                    metrics.map(([key], metricIndex) => [
                      key,
                      values[(index + metricIndex) % values.length],
                    ]),
                  ),
                },
              ],
            })),
          },
        }),
      )
      await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
      await page.getByRole("tab", { name: "广告", exact: true }).click()
      await page
        .getByRole("button", { name: /嘉书-总裁归来/ })
        .first()
        .click()
      const sheet = page.getByRole("dialog")
      await sheet.getByRole("tab", { name: "素材" }).click()
      const names = sheet.locator("tbody tr td[title]")
      // 各指标把同组测试数值错开，确保切换指标后确实读取了对应字段。
      const orderedNames = (groups: number[][], offset: number) =>
        groups.flatMap((ids) =>
          ids
            .map(
              (id) => ((id - 1 - offset + values.length) % values.length) + 1,
            )
            .sort((a, b) => a - b)
            .map((id) => `ep${id}.mp4`),
        )
      // 同值与缺失值各自按原始素材顺序排列，错位后也保持稳定。
      const descending = [[2, 5], [1], [3], [4, 6, 7, 8]]
      const ascending = [[3], [1], [2, 5], [4, 6, 7, 8]]
      await expect(names).toHaveText(orderedNames(descending, 0))
      if (role === "operator")
        await sheet.getByRole("checkbox", { name: "选择 ep2.mp4" }).check()
      for (const [metricIndex, [key, label]] of metrics.entries()) {
        const head = sheet.getByRole("columnheader").filter({
          has: page.getByRole("button", { name: new RegExp(`^${label}，`) }),
        })
        const button = head.getByRole("button")
        // 首列已默认按消耗降序；换到其他指标时首次点击也为降序。
        if (key !== "spend") await button.click()
        await expect(head).toHaveAttribute("aria-sort", "descending")
        await expect(names).toHaveText(orderedNames(descending, metricIndex))
        await button.click()
        await expect(head).toHaveAttribute("aria-sort", "ascending")
        await expect(names).toHaveText(orderedNames(ascending, metricIndex))
        await button.click()
        await expect(names).toHaveText(orderedNames(descending, metricIndex))
      }
      if (role === "operator") {
        await expect(
          sheet.getByRole("checkbox", { name: "选择 ep2.mp4" }),
        ).toBeChecked()
        const freeze = page.waitForRequest((r) =>
          r.url().includes("/ads/ad/ad-1/material-selections"),
        )
        await sheet
          .getByRole("button", { name: "暂停", exact: true })
          .last()
          .click()
        expect((await freeze).postDataJSON()).toEqual({
          ad_material_ids: ["m2"],
        })
      } else {
        await expect(sheet.getByRole("checkbox")).toHaveCount(0)
      }
    })
  }

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
  test("小时数据按需拉取期间提示并自动刷新", async ({ page }) => {
    await adsBoundary(page, { hourSyncingCalls: 1 })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page
      .getByRole("button", { name: /嘉书-总裁归来/ })
      .first()
      .click()
    const sheet = page.getByRole("dialog")
    await sheet.getByRole("tab", { name: "趋势" }).click()
    await sheet.getByRole("button", { name: "按小时" }).click()
    await expect(
      sheet.getByText("正在拉取小时数据，完成后自动更新"),
    ).toBeVisible()
    // 轮询拿到数据后提示消失、小时点出现，无需用户手动刷新。
    await expect(
      sheet.getByRole("columnheader", { name: "时间（UTC）" }),
    ).toBeVisible({ timeout: 10_000 })
    await expect(
      sheet.getByText("正在拉取小时数据，完成后自动更新"),
    ).toHaveCount(0)
  })
})
