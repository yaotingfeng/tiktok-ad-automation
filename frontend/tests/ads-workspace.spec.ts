import { expect, test } from "@playwright/test"
import {
  ADVERTISER,
  adsBoundary,
  BC_A,
  BC_B,
  TENANT,
} from "./utils/adsBoundary"

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
      "广告展示总价值",
      "广告收益 ROAS",
      "第 0 天 广告收益金额",
      "第 0 天广告收益 ROAS",
      "第 2 天广告收益 ROAS",
      "第 6 天广告收益 ROAS",
      "第 13 天广告收益 ROAS",
      "第 29 天广告收入 ROAS",
      "展示量",
      "点击量（目标页面）",
      "点击率（目标页面）",
      "千次展示成本 (CPM)",
      "平均点击成本（目标页面）",
      "广告展示事件总数",
      "单次广告展示事件成本",
    ]) {
      await expect(
        page.getByRole("columnheader", {
          // 默认按消耗降序，消耗列表头显示当前方向。
          name: column === "消耗" ? "消耗，当前降序" : `${column}，点击排序`,
          exact: true,
        }),
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
    await expect(page.getByText("乙账户", { exact: true })).toBeVisible()
    await page.waitForTimeout(900)
    await expect(page.getByText("甲账户", { exact: true })).toHaveCount(0)
  })

  test("按账户到素材逐级跳转并固定账户", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)

    const navigate = async (name: string, dimension: string, id: string) => {
      const request = page.waitForRequest((value) => {
        const url = new URL(value.url())
        return (
          value.method() === "GET" &&
          url.pathname.endsWith(`/tenants/${TENANT}/ads`) &&
          url.searchParams.get("dimension") === dimension
        )
      })
      await page
        .locator("tbody tr")
        .first()
        .getByRole("button", { name: `查看 ${name} 的下级`, exact: true })
        .click()
      const url = new URL((await request).url())
      expect(url.searchParams.get("ids")).toBe(id)
      expect(url.searchParams.get("advertiser_id")).toBe(ADVERTISER)
      const labels: Record<string, string> = {
        campaign: "系列",
        adgroup: "广告组",
        ad: "广告",
        material: "素材",
      }
      await expect(
        page.getByRole("tab", { name: labels[dimension], exact: true }),
      ).toHaveAttribute("data-state", "active")
    }

    await navigate("甲账户", "campaign", ADVERTISER)
    await navigate("嘉书-总裁归来-测试", "adgroup", "campaign-1")
    await navigate("嘉书-总裁归来-测试", "ad", "adgroup-1")
    await navigate("嘉书-总裁归来-测试", "material", "ad-1")
    // 素材是最末级，不再提供进入下级的按钮。
    await expect(
      page
        .locator("tbody tr")
        .first()
        .getByRole("button", { name: /的下级$/ }),
    ).toHaveCount(0)
  })

  test("点击上级路径按该上级筛选", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告", exact: true }).click()
    const row = page.locator("tbody tr").first()
    await expect(row.getByText("ad-1", { exact: true })).toBeVisible()
    const request = page.waitForRequest((value) => {
      const url = new URL(value.url())
      return (
        url.pathname.endsWith(`/tenants/${TENANT}/ads`) &&
        url.searchParams.get("dimension") === "campaign"
      )
    })
    await row.getByRole("button", { name: "甲账户", exact: true }).click()
    const url = new URL((await request).url())
    expect(url.searchParams.get("ids")).toBe(ADVERTISER)
    expect(url.searchParams.get("advertiser_id")).toBe(ADVERTISER)
    await expect(
      page.getByRole("tab", { name: "系列", exact: true }),
    ).toHaveAttribute("data-state", "active")
  })

  test("相同筛选应用也清空选择", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("checkbox", { name: "选择 甲账户" }).check()
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
        url.searchParams.get("bc_id") === BC_B
      )
        bcBRequests.push(request.url())
    })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await page.getByRole("button", { name: "下一页" }).click()
    await expect(
      page.getByText("嘉书-第二页-测试", { exact: true }),
    ).toBeVisible()
    await page.getByRole("combobox", { name: "当前 BC" }).click()
    await page.getByText("乙 BC", { exact: true }).click()
    // 切 BC 会重新进入工作台，回到默认账户维度。
    await expect(page.getByText("乙账户", { exact: true })).toBeVisible()
    const bcB = new URL(bcBRequests.at(-1) ?? "http://localhost")
    expect(bcB.searchParams.get("cursor")).toBeNull()
    expect(bcB.searchParams.get("snapshot_id")).toBeNull()
  })

  test("默认账户维度，横向滚动后勾选框和名称列仍固定", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(
      page.getByRole("tab", { name: "账户", selected: true }),
    ).toBeVisible()
    const container = page.locator('[data-slot="table-container"]')
    // 新增趋势区域后表格可能在视口下方，滚动到视图内再测量
    await container.scrollIntoViewIfNeeded()
    const nameCell = page.locator('[data-sticky="name"]').first()
    const before = await nameCell.boundingBox()
    await container.evaluate((el) => el.scrollTo({ left: 800 }))
    const after = await nameCell.boundingBox()
    expect(after?.x).toBe(before?.x)
    await expect(
      page.locator('[data-sticky="select"]').first(),
    ).toBeInViewport()
  })

  test("不再出现详情与 TikTok 后台列", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("columnheader", { name: "详情" })).toHaveCount(
      0,
    )
    await expect(page.getByRole("link", { name: "TikTok 后台" })).toHaveCount(0)
    await expect(
      page.getByRole("columnheader", { name: "数据状态" }),
    ).toHaveCount(0)
  })

  test("素材维度隐藏平台不提供的天数列", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "素材" }).click()
    await expect(
      page.getByRole("columnheader", { name: /第 2 天/ }),
    ).toHaveCount(0)
    await expect(
      page.getByRole("columnheader", { name: /广告曝光总价值/ }),
    ).toBeVisible()
  })

  test("状态显示中文徽标，原始枚举放在悬停说明", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByText("正常", { exact: true })).toHaveAttribute(
      "title",
      "STATUS_ENABLE",
    )
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await expect(page.getByText("已暂停", { exact: true })).toBeVisible()
    // 系列维度带预算配置列，账户维度带余额列。
    await expect(
      page.getByRole("columnheader", { name: /^预算/ }),
    ).toBeVisible()
    await expect(
      page.getByRole("cell", { name: "50.00", exact: true }),
    ).toBeVisible()
  })

  test("列为部分同步时照常显示数值并给出标记", async ({ page }) => {
    await adsBoundary(page, { partialSpend: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    const row = page.locator("tbody tr").first()
    await expect(row.getByText("100.00", { exact: true })).toBeVisible()
    await expect(row.getByLabel("部分账户数据未同步完整")).toBeVisible()
    // 行覆盖完整时名称旁不出现不完整警示。
    await expect(row.getByLabel("数据不完整")).toHaveCount(0)
  })

  test("回收窗口未结束的天数列表头标灰", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    // 默认日期为今天，第 N 天窗口全部未结束；总值列不受影响。
    await expect(
      page.locator('th[data-recovery-open="true"]').first(),
    ).toBeVisible()
    await expect(page.locator('th[data-metric="roas_d30"]')).toHaveAttribute(
      "data-recovery-open",
      "true",
    )
    await expect(
      page.locator('th[data-metric="roas_total"]'),
    ).not.toHaveAttribute("data-recovery-open", "true")
  })

  test("汇总卡片使用官方名并提示不完整账户", async ({ page }) => {
    await adsBoundary(page, { incomplete: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    // 卡片在表格之前，用 first() 选中卡片区的按钮
    for (const name of [
      "消耗",
      "广告展示总价值",
      "广告收益 ROAS",
      "第 0 天广告收益 ROAS",
      "展示量",
      "点击率（目标页面）",
    ])
      await expect(
        page.getByRole("button", { name: new RegExp(`^${name}`) }).first(),
      ).toBeVisible()
    // CPM 的括号需要转义
    await expect(
      page.getByRole("button", { name: /^千次展示成本/ }).first(),
    ).toBeVisible()
    await expect(page.getByText("2 个账户数据不完整")).toBeVisible()
  })

  test("趋势默认折叠，展开才请求", async ({ page }) => {
    const trendCalls = { count: 0 }
    await adsBoundary(page, { trendCalls })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("heading", { name: "广告报表" })).toBeVisible()
    expect(trendCalls.count).toBe(0)
    await page.getByRole("button", { name: "展开趋势" }).click()
    await expect(page.getByTestId("ads-trend-chart")).toBeVisible()
    expect(trendCalls.count).toBe(1)
  })

  test("隐藏列在刷新后保持", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("button", { name: "列设置" }).click()
    await page.getByRole("menuitemcheckbox", { name: "千次展示成本 (CPM)" }).click()
    await page.keyboard.press("Escape")
    await expect(page.getByRole("columnheader", { name: /千次展示成本/ })).toHaveCount(0)
    await page.reload()
    await expect(page.getByRole("columnheader", { name: /千次展示成本/ })).toHaveCount(0)
  })

  test("保存筛选提交字典列键", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("columnheader", { name: /消耗/ })).toBeVisible()
    const request = page.waitForRequest((r) => new URL(r.url()).pathname.endsWith("/report-views") && r.method() === "POST")
    await page.getByRole("button", { name: "保存筛选" }).click()
    const body = (await request).postDataJSON()
    expect(body.columns).toContain("roas_d0")
    expect(body.columns).not.toContain("status_raw")
  })

  test("表头固定，垂直滚动后位置不变", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    const container = page.locator('[data-slot="table-container"]')
    await container.scrollIntoViewIfNeeded()
    const header = page.locator('[data-sticky="name"]').first()
    const before = (await header.boundingBox())?.y
    await container.evaluate((el) => el.scrollTo({ top: 200 }))
    const after = (await header.boundingBox())?.y
    expect(after).toBe(before)
  })
})
