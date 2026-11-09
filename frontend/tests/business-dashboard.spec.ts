import { expect, type Page, test } from "@playwright/test"
import { automationBoundary, TENANT } from "./utils/automationBoundary"

const BC = "90071992547409931111"
const PROVIDER = "22222222-2222-4222-8222-222222222222"
const values = {
  spend: "2000",
  revenue: "1600",
  provider_share: "128",
  net_revenue: "1472",
  rebate: "120",
  business_revenue: "1592",
  compensation: "100",
  subsidy_comic: "800",
  subsidy_other: "500",
  subsidy_existing: "400",
  platform_roas: "0.8",
  business_roas: "0.796",
  scenario_revenue: "1592",
  scenario_roas: "0.796",
}

async function boundary(
  page: Page,
  role: "tenant_admin" | "viewer" | "operator" = "tenant_admin",
) {
  await automationBoundary(page, { role })
  const queries: Record<string, unknown>[] = []
  const rates: Record<string, unknown>[] = []
  const previews: Record<string, unknown>[] = []
  let schedule = { revision: 0, periods: [] as Record<string, unknown>[] }
  const evidence: Record<string, unknown>[] = []
  await page.route("**/api/**/business/**", async (route) => {
    const path = new URL(route.request().url()).pathname
    const body =
      route.request().method() === "GET" ? {} : route.request().postDataJSON()
    const reply = (data: unknown, status = 200) =>
      route.fulfill({ status, json: data })
    if (path.endsWith("/options"))
      return reply({
        end_date: "2026-11-03",
        accounts: [
          { id: "acc-1", name: "投放账户", currency: "USD", timezone: "UTC" },
        ],
        providers: [{ id: PROVIDER, name: "嘉书" }],
      })
    if (path.endsWith("/query")) {
      queries.push(body)
      const scenario = body.scenario ?? "base"
      const extra =
        scenario === "comic"
          ? 900
          : scenario === "other"
            ? 600
            : scenario === "existing"
              ? 500
              : 0
      const output = {
        ...values,
        scenario_revenue: String(1592 + extra),
        scenario_roas: String((1592 + extra) / 2000),
      }
      return reply({
        rows: [
          {
            key: "drama-1",
            name:
              body.dimension === "provider"
                ? "嘉书"
                : body.dimension === "campaign"
                  ? "测试系列"
                  : "测试剧",
            provider_name: "嘉书",
            provider_id: PROVIDER,
            advertiser_id: "acc-1",
            campaign_id: "campaign-1",
            drama_id: PROVIDER,
            values: output,
          },
        ],
        totals: output,
        total: 1,
        run_ids: [PROVIDER],
        currency: "USD",
        currencies: ["USD"],
        end_date: "2026-11-03",
        updated_at: "2026-11-04T01:00:00Z",
        issues: [],
        missing_dates: [],
        pending: false,
      })
    }
    if (path.endsWith("/rates/preview")) {
      previews.push(body)
      return reply({
        revision: schedule.revision,
        before: schedule.periods,
        periods: [
          {
            start_date: body.start_date,
            end_date: body.end_date ?? null,
            rate: body.rate ?? null,
          },
        ],
      })
    }
    if (path.endsWith("/rates/history"))
      return reply({ items: [], has_more: false })
    if (path.endsWith("/rates")) {
      if (route.request().method() === "POST") {
        rates.push(body)
        schedule = {
          revision: schedule.revision + 1,
          periods: [
            {
              start_date: body.start_date,
              end_date: body.end_date ?? null,
              rate: body.rate ?? null,
            },
          ],
        }
      }
      return reply(schedule)
    }
    if (path.endsWith("/evidence")) {
      if (route.request().method() === "PUT") {
        evidence.push(body)
        return reply({ saved: true })
      }
      return reply({
        bid: null,
        eligible: null,
        source: "",
        evidence_revision: "a".repeat(64),
        status: "UNKNOWN",
        reasons: ["BID_CHAIN_AMBIGUOUS"],
      })
    }
    if (path.endsWith("/rebuild")) return reply({ run_ids: [PROVIDER] }, 202)
    if (path.endsWith("/runs")) return reply([])
    if (path.endsWith("/exports"))
      return reply(
        { id: PROVIDER, status: "QUEUED", expires_at: "2026-11-05T00:00:00Z" },
        202,
      )
    if (path.endsWith(`/exports/${PROVIDER}`))
      return reply({
        id: PROVIDER,
        status: "COMPLETE",
        expires_at: "2026-11-05T00:00:00Z",
      })
    if (path.endsWith("/download"))
      return route.fulfill({
        body: "名称,消耗\n测试剧,2000",
        contentType: "text/csv",
      })
    return reply({ detail: path }, 404)
  })
  return { queries, rates, previews, evidence }
}

test("经营看板五卡与三个补贴情景独立展示", async ({ page }) => {
  const calls = await boundary(page)
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await expect(
    page.getByRole("heading", { name: "经营看板", exact: true }),
  ).toBeVisible()
  for (const label of [
    "平台消耗",
    "平台收入",
    "平台 ROAS",
    "经营收入",
    "经营 ROAS",
  ])
    await expect(
      page.getByTestId("business-cards").getByText(label, { exact: true }),
    ).toBeVisible()
  for (const label of ["新开漫剧预计", "新开其他剧预计", "在投闭环预计"])
    await expect(page.getByRole("columnheader", { name: label })).toBeVisible()
  await page.getByRole("combobox", { name: "经营口径" }).click()
  await page.getByRole("option", { name: "含预计：新开漫剧" }).click()
  await expect(page.getByTestId("business-cards")).toContainText("2,492.00")
  expect(calls.queries.at(-1)?.scenario).toBe("comic")
  await expect(page.getByRole("listbox")).toHaveCount(0)
  await page.screenshot({
    path: test.info().outputPath("dashboard-desktop.png"),
    fullPage: true,
  })
  await page.getByRole("radio", { name: "广告系列", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "测试系列", exact: true }),
  ).toBeVisible()
  await page.getByRole("radio", { name: "版权方", exact: true }).click()
  await expect.poll(() => calls.queries.at(-1)?.dimension).toBe("provider")
})

test("返点默认0且按生效日保存，小屏保持页面可用", async ({ page }) => {
  const calls = await boundary(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await page.getByRole("button", { name: "经营设置", exact: true }).click()
  await expect(
    page.getByRole("spinbutton", { name: "其他返点比例（%）" }),
  ).toHaveValue("0")
  await page.getByRole("spinbutton", { name: "其他返点比例（%）" }).fill("10.8")
  await page
    .getByRole("dialog")
    .getByLabel("开始日期", { exact: true })
    .fill("2026-10-10")
  await page.getByRole("button", { name: "预览调整", exact: true }).click()
  await page.getByRole("button", { name: "确认保存", exact: true }).click()
  await expect.poll(() => calls.rates.length).toBe(1)
  expect(calls.rates[0]).toMatchObject({
    kind: "rebate",
    scope_id: BC,
    start_date: "2026-10-10",
    rate: "0.108",
  })
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true)
  await page.screenshot({
    path: test.info().outputPath("dashboard-mobile.png"),
    fullPage: true,
  })
})

for (const role of ["viewer", "operator"] as const) {
  test(`${role}不能查看经营看板或导出`, async ({ page }) => {
    await boundary(page, role)
    const requests: string[] = []
    page.on("request", (request) => {
      if (
        new URL(request.url()).pathname.startsWith("/api/") &&
        request.url().includes("/business/")
      )
        requests.push(request.url())
    })
    await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
    await expect(page.getByText("仅管理员可以查看经营报表。")).toBeVisible()
    await expect(
      page.getByRole("link", { name: "经营看板", exact: true }),
    ).toHaveCount(0)
    await expect(page.getByRole("button", { name: "导出 CSV" })).toHaveCount(0)
    await expect(page.getByText("2,000.00", { exact: true })).toHaveCount(0)
    expect(requests).toEqual([])
  })
}

test("系列赔付按指定当地日绑定当前自动依据更正", async ({ page }) => {
  const calls = await boundary(page)
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await page.getByRole("radio", { name: "广告系列", exact: true }).click()
  await page.getByRole("button", { name: "测试系列操作", exact: true }).click()
  await page.getByRole("menuitem", { name: "赔付依据", exact: true }).click()
  await page.getByLabel("核算日期", { exact: true }).fill("2026-11-02")
  await page.getByLabel("当日最低 ROAS 出价", { exact: true }).fill("1.06")
  await page.getByRole("combobox", { name: "当日核验依据" }).click()
  await page.getByRole("option", { name: "已核实适用且无排除操作" }).click()
  await page
    .getByLabel("依据说明", { exact: true })
    .fill("测试历史配置与操作核验记录")
  await page.getByRole("button", { name: "保存依据", exact: true }).click()
  await expect.poll(() => calls.evidence.length).toBe(1)
  expect(calls.evidence[0]).toMatchObject({
    bc_id: BC,
    advertiser_id: "acc-1",
    campaign_id: "campaign-1",
    stat_date: "2026-11-02",
    bid: "1.06",
    eligible: true,
    source: "测试历史配置与操作核验记录",
    evidence_revision: "a".repeat(64),
  })
})

test("版权方连接中独立保存抽成，不触发凭据验证", async ({ page }) => {
  const calls = await boundary(page)
  const verifications: string[] = []
  await page.route("**/api/**/providers/connections**", async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith("/verify")) {
      verifications.push(path)
      return route.fulfill({ status: 500 })
    }
    return route.fulfill({
      json: {
        items: [
          {
            id: PROVIDER,
            display_name: "嘉书连接",
            kind: "jiashu",
            status: "active",
            error_code: null,
            verified_at: null,
          },
        ],
        total: 1,
        next_cursor: null,
      },
    })
  })
  await page.goto(`/tenants/${TENANT}/providers?bc_id=${BC}`)
  await page.getByRole("button", { name: "抽成设置", exact: true }).click()
  await expect(
    page.getByRole("spinbutton", { name: "版权方抽成比例（%）" }),
  ).toHaveValue("0")
  await page.getByRole("spinbutton", { name: "版权方抽成比例（%）" }).fill("8")
  await page
    .getByRole("dialog")
    .getByLabel("开始日期", { exact: true })
    .fill("2026-10-01")
  await page.getByRole("button", { name: "预览调整", exact: true }).click()
  await page.getByRole("button", { name: "确认保存", exact: true }).click()
  await expect.poll(() => calls.rates.length).toBe(1)
  expect(calls.rates[0]).toMatchObject({
    kind: "provider",
    scope_id: PROVIDER,
    start_date: "2026-10-01",
    rate: "0.08",
  })
  expect(verifications).toEqual([])
})

test("13寸屏幕保留列表空间，说明按需查看且列可排序", async ({ page }) => {
  const calls = await boundary(page)
  await page.setViewportSize({ width: 1366, height: 768 })
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await expect(
    page.getByRole("button", { name: "测试剧", exact: true }),
  ).toBeVisible()
  for (const width of [1366, 1280]) {
    await page.setViewportSize({ width, height: 720 })
    const cards = await page.getByTestId("business-cards").boundingBox()
    expect(cards!.height).toBeLessThan(110)
    const table = await page.getByTestId("business-detail-table").boundingBox()
    expect(table!.y).toBeLessThan(400)
  }
  await expect(
    page.getByText("合计收入 ÷ 合计消耗", { exact: true }),
  ).toHaveCount(0)
  await page
    .getByTestId("business-cards")
    .getByRole("button", { name: "平台 ROAS说明", exact: true })
    .hover()
  await expect(page.getByRole("tooltip")).toContainText("平台总收入 ÷ 总消耗")
  await page
    .getByRole("button", { name: "平台 ROAS，点击排序", exact: true })
    .click()
  await expect.poll(() => calls.queries.at(-1)?.sort_by).toBe("platform_roas")
  await expect.poll(() => calls.queries.at(-1)?.descending).toBe(true)
  await page
    .getByRole("button", { name: "平台 ROAS，当前降序", exact: true })
    .click()
  await expect.poll(() => calls.queries.at(-1)?.descending).toBe(false)
  await page.getByRole("button", { name: "数据说明", exact: true }).click()
  await expect(
    page.getByRole("dialog", { name: "数据说明", exact: true }),
  ).toContainText("账户当地日期")
  await page.keyboard.press("Escape")
  await expect(
    page.getByRole("dialog", { name: "数据说明", exact: true }),
  ).toHaveCount(0)
  await page.mouse.move(0, 0)
  await page.screenshot({
    path: test.info().outputPath("dashboard-13inch.png"),
    fullPage: true,
  })
})

test("比例先预览再保存，返回修改和取消不写入", async ({ page }) => {
  const calls = await boundary(page)
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await page.getByRole("button", { name: "经营设置", exact: true }).click()
  await page
    .getByRole("dialog")
    .getByLabel("开始日期", { exact: true })
    .fill("2026-08-01")
  await page.getByLabel("其他返点比例（%）").fill("6")
  await page.getByLabel("持续有效", { exact: true }).uncheck()
  await page
    .getByRole("dialog")
    .getByLabel("结束日期", { exact: true })
    .fill("2026-09-30")
  await page.getByRole("button", { name: "预览调整", exact: true }).click()
  await expect(
    page.getByRole("heading", { name: "保存后的时间段" }),
  ).toBeVisible()
  expect(calls.rates).toHaveLength(0)
  await expect(page.getByText("2026-09-30", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "返回修改", exact: true }).click()
  await expect(
    page.getByRole("dialog").getByLabel("结束日期", { exact: true }),
  ).toHaveValue("2026-09-30")
  await page.getByRole("button", { name: "预览调整", exact: true }).click()
  await page.getByRole("button", { name: "确认保存", exact: true }).click()
  await expect.poll(() => calls.rates.length).toBe(1)
  expect(calls.rates[0]).toMatchObject({
    expected_revision: 0,
    start_date: "2026-08-01",
    end_date: "2026-09-30",
    rate: "0.06",
  })
})

test("结束时间段默认接续到下一配置之前", async ({ page }) => {
  await boundary(page)
  await page.route("**/business/rates?**", (route) =>
    route.fulfill({
      json: {
        revision: 2,
        periods: [
          { start_date: "2026-08-01", end_date: "2026-09-30", rate: "0.06" },
          { start_date: "2026-10-01", end_date: null, rate: "0.04" },
        ],
      },
    }),
  )
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await page.getByRole("button", { name: "经营设置", exact: true }).click()
  const row = page.getByRole("row").filter({ hasText: "2026-08-01" })
  await row.getByRole("button", { name: "结束", exact: true }).click()
  await page.getByLabel("截止日期", { exact: true }).fill("2026-08-31")
  await page.getByLabel("同时设置接续比例").check()
  await expect(page.getByLabel("接续结束日期", { exact: true })).toHaveValue(
    "2026-09-30",
  )
  await expect(page.getByLabel("接续比例持续有效")).not.toBeChecked()
  await page.getByLabel("接续比例（%）").fill("5")
  await page.getByRole("button", { name: "预览调整", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "确认保存", exact: true }),
  ).toBeVisible()
})

test("修改时间段保留原段标识，预览明确待配置空档", async ({ page }) => {
  await boundary(page)
  const original = [{ start_date: "2026-08-01", end_date: null, rate: "0.06" }]
  let operation: Record<string, unknown> = {}
  await page.route("**/business/rates**", async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith("/rates") && route.request().method() === "GET")
      return route.fulfill({ json: { revision: 1, periods: original } })
    if (url.pathname.endsWith("/preview")) {
      operation = route.request().postDataJSON()
      return route.fulfill({
        json: {
          revision: 1,
          before: original,
          periods: [
            { start_date: "2026-08-01", end_date: "2026-08-14", rate: null },
            { start_date: "2026-08-15", end_date: "2026-08-31", rate: "0.05" },
            { start_date: "2026-09-01", end_date: null, rate: null },
          ],
        },
      })
    }
    return route.fallback()
  })
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await page.getByRole("button", { name: "经营设置", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByRole("button", { name: "修改", exact: true }).click()
  await dialog.getByLabel("开始日期", { exact: true }).fill("2026-08-15")
  await dialog.getByLabel("持续有效", { exact: true }).uncheck()
  await dialog.getByLabel("结束日期", { exact: true }).fill("2026-08-31")
  await dialog.getByLabel("其他返点比例（%）").fill("5")
  await dialog.getByRole("button", { name: "预览调整", exact: true }).click()
  await expect(
    dialog.getByText("存在待配置时间段", { exact: true }),
  ).toBeVisible()
  expect(operation).toMatchObject({
    action: "edit",
    target_start_date: "2026-08-01",
    start_date: "2026-08-15",
    end_date: "2026-08-31",
    rate: "0.05",
    expected_revision: 1,
  })
  await page.screenshot({
    path: test.info().outputPath("rate-edit-preview.png"),
    fullPage: true,
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(
    dialog.getByRole("button", { name: "确认保存", exact: true }),
  ).toBeVisible()
  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    )
    .toBe(true)
  await page.screenshot({
    path: test.info().outputPath("rate-edit-preview-mobile.png"),
    fullPage: true,
  })
})

test("并发修改后保留输入，刷新修订再预览保存", async ({ page }) => {
  await boundary(page)
  let revision = 1
  let attempts = 0
  const previews: Record<string, unknown>[] = []
  await page.route("**/business/rates**", async (route) => {
    const url = new URL(route.request().url())
    const periods = [{ start_date: "2026-10-01", end_date: null, rate: "0.04" }]
    if (url.pathname.endsWith("/rates") && route.request().method() === "GET")
      return route.fulfill({ json: { revision, periods } })
    if (url.pathname.endsWith("/preview")) {
      previews.push(route.request().postDataJSON())
      return route.fulfill({
        json: {
          revision,
          before: periods,
          periods: [{ start_date: "2026-08-01", end_date: null, rate: "0.06" }],
        },
      })
    }
    if (
      url.pathname.endsWith("/rates") &&
      route.request().method() === "POST"
    ) {
      attempts++
      if (attempts === 1) {
        revision = 2
        return route.fulfill({
          status: 409,
          json: { detail: "比例配置已被修改" },
        })
      }
      return route.fulfill({ json: { revision: 3, periods } })
    }
    return route.fallback()
  })
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await page.getByRole("button", { name: "经营设置", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByLabel("开始日期", { exact: true }).fill("2026-08-01")
  await dialog.getByLabel("其他返点比例（%）").fill("6")
  await dialog.getByRole("button", { name: "预览调整", exact: true }).click()
  await dialog.getByRole("button", { name: "确认保存", exact: true }).click()
  await expect(dialog.getByRole("alert")).toContainText("你的输入已保留")
  await expect(dialog.getByLabel("其他返点比例（%）")).toHaveValue("6")
  await expect(dialog.getByLabel("开始日期", { exact: true })).toHaveValue(
    "2026-08-01",
  )
  await dialog.getByRole("button", { name: "预览调整", exact: true }).click()
  expect(previews.map((p) => p.expected_revision)).toEqual([1, 2])
  await dialog.getByRole("button", { name: "确认保存", exact: true }).click()
  await expect(dialog).toHaveCount(0)
})

test("修改记录展示操作人和调整前后日程", async ({ page }) => {
  await boundary(page)
  await page.route("**/business/rates/history?**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: PROVIDER,
            revision: 2,
            actor_id: PROVIDER,
            actor_name: "结算管理员",
            created_at: "2026-11-09T08:00:00Z",
            note: "三季度达量优惠",
            legacy: false,
            previous_periods: [
              { start_date: "2026-08-01", end_date: null, rate: "0.1" },
            ],
            periods: [
              {
                start_date: "2026-08-01",
                end_date: "2026-09-30",
                rate: "0.06",
              },
              { start_date: "2026-10-01", end_date: null, rate: null },
            ],
          },
        ],
        has_more: false,
      },
    }),
  )
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await page.getByRole("button", { name: "经营设置", exact: true }).click()
  await page.getByRole("button", { name: "查看修改记录" }).click()
  await page.getByText(/修订 2 ·/).click()
  await expect(
    page.getByText("操作人：结算管理员", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("三季度达量优惠", { exact: true })).toBeVisible()
  await expect(page.getByText("调整前", { exact: true })).toBeVisible()
  await expect(page.getByText("调整后", { exact: true })).toBeVisible()
})

test("已有7%比例修改日期时保留精确输入", async ({ page }) => {
  const calls = await boundary(page)
  await page.route("**/business/rates?**", (route) =>
    route.fulfill({
      json: {
        revision: 1,
        periods: [{ start_date: "2026-08-01", end_date: null, rate: "0.07" }],
      },
    }),
  )
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await page.getByRole("button", { name: "经营设置", exact: true }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByRole("button", { name: "修改", exact: true }).click()
  await expect(dialog.getByLabel("其他返点比例（%）")).toHaveValue("7")
  await dialog.getByLabel("持续有效", { exact: true }).uncheck()
  await dialog.getByLabel("结束日期", { exact: true }).fill("2026-09-30")
  await dialog.getByRole("button", { name: "预览调整", exact: true }).click()
  await expect(
    dialog.getByRole("heading", { name: "保存后的时间段" }),
  ).toBeVisible()
  expect(calls.previews[0]).toMatchObject({
    rate: "0.07",
    start_date: "2026-08-01",
    end_date: "2026-09-30",
  })
})

test("赔付依据缺失时显示小计且不可无版本覆盖", async ({ page }) => {
  await boundary(page)
  await page.route("**/business/query", (route) =>
    route.fulfill({
      json: {
        rows: [
          {
            key: "campaign-test",
            name: "测试系列",
            provider_name: "嘉书",
            advertiser_id: "acc-1",
            campaign_id: "campaign-1",
            values,
          },
        ],
        totals: values,
        total: 1,
        run_ids: [PROVIDER],
        currency: "USD",
        currencies: ["USD"],
        end_date: "2026-11-03",
        issues: ["COMPENSATION_PARTIAL", "COMPENSATION_QUALIFICATION_PENDING"],
        missing_dates: [],
        pending: false,
      },
    }),
  )
  await page.route("**/business/evidence?**", (route) =>
    route.fulfill({
      json: {
        bid: null,
        eligible: null,
        source: "",
        evidence_revision: null,
        status: "UNKNOWN",
        reasons: [],
      },
    }),
  )
  await page.goto(`/tenants/${TENANT}/business?bc_id=${BC}`)
  await expect(page.getByText("预计赔付小计", { exact: true })).toBeVisible()
  await page.getByRole("radio", { name: "广告系列", exact: true }).click()
  await page.getByRole("button", { name: "测试系列操作", exact: true }).click()
  await page.getByRole("menuitem", { name: "赔付依据", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "保存依据", exact: true }),
  ).toBeDisabled()
  await expect(
    page.getByText("自动依据正在补齐，取得依据后可提交本次更正。", {
      exact: true,
    }),
  ).toBeVisible()
})
