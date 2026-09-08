import { expect, type Page, test } from "@playwright/test"
import type { StrategyPublic, VersionPublic } from "../src/client"

const A = "11111111-1111-4111-8111-111111111111",
  B = "22222222-2222-4222-8222-222222222222",
  S = "294999ce-f767-45c6-8da0-d2cbeaf71963",
  V = "33333333-3333-4333-8333-333333333333",
  U = "44444444-4444-4444-8444-444444444444",
  POOL = "02a4e656-a330-40dc-864c-26e81961f3ca"
const config = {
  budget: "100.00",
  currency: "USD",
  target_roas: "1.08",
  group_size: 10,
  creative_count: 2,
  copy_pool_version: POOL,
  cta_option_ids: ["official-existing-id"],
  campaign_suffix: "-{YYYYMMDD}-{batch_short_id}",
}
const original: StrategyPublic = {
  id: S,
  name: "租户策略",
  active: true,
  latest_version: 1,
  version_id: V,
  config,
  created_by: U,
  created_at: "2026-09-08T08:00:00Z",
}
async function boundary(
  page: Page,
  options: {
    role?: "operator" | "viewer" | "tenant_admin"
    count?: number
    capacity?: number
    mode?: "conflict" | "unknown" | "unknown404" | "failure"
    empty?: boolean
    deny?: boolean
    loggedOut?: boolean
  } = {},
) {
  const requests: {
      path: string
      method: string
      query: URLSearchParams
      body: any
    }[] = [],
    records = options.empty
      ? []
      : Array.from({ length: options.count || 1 }, (_, i) => ({
          ...structuredClone(original),
          id:
            i === 0
              ? S
              : `88888888-8888-4888-8888-${String(i).padStart(12, "0")}`,
          name: i === 0 ? "租户策略" : `分页策略 ${i + 1}`,
        }))
  const versions: VersionPublic[] = [
      {
        id: V,
        strategy_id: S,
        number: 1,
        config: structuredClone(config),
        created_by: U,
        created_at: original.created_at,
        request_id: "55555555-5555-4555-8555-555555555555",
      },
    ],
    saved = new Map<string, VersionPublic>()
  await page.addInitScript((loggedOut) => {
    if (loggedOut && !sessionStorage.getItem("strategy-fixture-initialized")) {
      localStorage.removeItem("access_token")
      sessionStorage.setItem("strategy-fixture-initialized", "true")
    } else if (!loggedOut)
      localStorage.setItem("access_token", "strategy-test-token")
  }, options.loggedOut)
  await page.route("**/api/**", async (route) => {
    const req = route.request(),
      url = new URL(req.url()),
      path = url.pathname,
      method = req.method(),
      query = url.searchParams
    const headers = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "*",
      "Access-Control-Allow-Methods": "*",
    }
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers })
    if (path === "/api/login/access-token")
      return route.fulfill({
        json: { access_token: "strategy-test-token", token_type: "bearer" },
        headers,
      })
    expect(req.headers().authorization).toBe("Bearer strategy-test-token")
    const body = req.postData() ? req.postDataJSON() : undefined
    requests.push({ path, method, query, body })
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ json, status, headers })
    const pageOf = (items: unknown[]) => {
      const size = Number(query.get("limit") || 50),
        offset = Number(query.get("cursor") || 0)
      return {
        items: items.slice(offset, offset + size),
        next_cursor:
          offset + size < items.length ? String(offset + size) : null,
      }
    }
    if (path === "/api/users/me")
      return reply({
        id: U,
        email: "operator@example.com",
        full_name: "策略测试用户",
        is_active: true,
        is_superuser: false,
      })
    if (path === "/api/me/tenants")
      return reply({
        items: [
          {
            id: A,
            name: "策略租户甲",
            active: true,
            role: options.role || "operator",
          },
          {
            id: B,
            name: "策略租户乙",
            active: true,
            role: options.role || "operator",
          },
        ].filter(
          (row) =>
            !query.get("search") ||
            row.id === query.get("search") ||
            row.name.includes(query.get("search")!),
        ),
        next_cursor: null,
      })
    if (path.endsWith("/bcs")) return reply({ items: [], next_cursor: null })
    if (options.deny)
      return reply(
        {
          code: "action_forbidden",
          message: "当前角色无权操作",
          retryable: false,
        },
        403,
      )
    if (path.includes("/copy-pools/"))
      return reply({
        id: POOL,
        name: "通用英文文案池 v1",
        entries: Array.from({ length: options.capacity ?? 100 }, (_, i) => ({
          id: `99999999-9999-4999-8999-${String(i).padStart(12, "0")}`,
          text: `Watch the story continue ${i + 1}.`,
          position: i + 1,
        })),
      })
    if (path.endsWith("/strategies/validate"))
      return reply({ valid: true, errors: [], scene_check_pending: true })
    if (path.includes("/strategy-save-requests/")) {
      const row = saved.get(path.split("/").pop()!)
      return row && options.mode !== "unknown404"
        ? reply(row)
        : reply(
            {
              code: "strategy_not_found",
              message: "尚无该保存记录",
              retryable: false,
            },
            404,
          )
    }
    if (path.includes("/strategy-versions/"))
      return reply(versions.find((row) => path.endsWith(row.id)))
    if (path.endsWith("/strategies") && method === "GET")
      return reply(
        pageOf(
          records.filter(
            (row) =>
              (!query.has("active") ||
                row.active === (query.get("active") === "true")) &&
              (!query.get("query") || row.name.includes(query.get("query")!)),
          ),
        ),
      )
    if (path.endsWith("/versions") && method === "GET")
      return reply(pageOf([...versions].reverse()))
    if (method === "GET")
      return reply(records.find((row) => path.endsWith(row.id)) || original)
    if (method === "PATCH") {
      const row = records.find((row) => path.endsWith(row.id))!
      row.active = body.active
      return reply(row)
    }
    if (method === "POST") {
      if (options.mode === "failure")
        return reply(
          {
            code: "copy_pool_exhausted",
            message: "文案数量不足",
            retryable: false,
          },
          422,
        )
      if (options.mode === "conflict") {
        records[0].latest_version = 2
        records[0].config = { ...config, budget: "120.00" }
        return reply(
          { code: "version_conflict", message: "版本已变化", retryable: false },
          409,
        )
      }
      const create = path.endsWith("/strategies"),
        strategyId = create ? "66666666-6666-4666-8666-666666666666" : S
      const row: VersionPublic = {
        id: "77777777-7777-4777-8777-777777777777",
        strategy_id: strategyId,
        number: create ? 1 : 2,
        config: body.config,
        created_by: U,
        created_at: "2026-09-09T08:00:00Z",
        request_id: body.request_id,
      }
      versions.push(row)
      saved.set(body.request_id, row)
      if (create)
        records.push({
          ...original,
          id: strategyId,
          name: body.name,
          config: body.config,
          version_id: row.id,
          latest_version: row.number,
        })
      else
        Object.assign(records[0], {
          version_id: row.id,
          config: body.config,
          latest_version: 2,
        })
      if (options.mode === "unknown" || options.mode === "unknown404")
        return route.abort("timedout")
      return reply(row, 201)
    }
    return reply({ message: `Unexpected fixture ${method} ${path}` }, 404)
  })
  return { requests, records, versions, saved }
}
const editUrl = `/tenants/${A}/strategies/${S}`
test("23 条素材的创意数量 2→3 只改变 Ad 数量，不倍增预算", async ({ page }) => {
  await boundary(page)
  await page.goto(editUrl)
  const example = page.getByRole("region", { name: "结构与预算示例" })
  await expect(example.getByText("6 条 Ad", { exact: true })).toBeVisible()
  await page.getByLabel("创意数量", { exact: true }).fill("3")
  await expect(
    example.getByText("3 个 Ad Group", { exact: true }),
  ).toBeVisible()
  await expect(example.getByText("9 条 Ad", { exact: true })).toBeVisible()
  await expect(
    example.getByText("USD 100.00 / Campaign / 天", { exact: true }),
  ).toBeVisible()
  await expect(page.getByLabel("自定义 CTA")).toHaveCount(0)
})
test("复制只读原版本；保存创建新策略使用真实 strategy_id 与版本号", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${A}/strategies`)
  await page.getByRole("button", { name: "复制为新策略", exact: true }).click()
  await expect(page.getByLabel("策略名称", { exact: true })).toHaveValue(
    "租户策略 副本",
  )
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
  await page.getByRole("button", { name: "创建策略", exact: true }).click()
  await expect(page).toHaveURL(
    /\/strategies\/66666666-6666-4666-8666-666666666666/,
  )
  await expect(page.getByText("已保存 v1", { exact: true })).toBeVisible()
  const writes = requests.filter(
    (r) => r.method === "POST" && !r.path.endsWith("/validate"),
  )
  expect(writes).toHaveLength(1)
  expect(writes[0].body.config.budget).toBe("100.00")
})
test("策略无有效改动时不产生重复版本，金额仅格式变化也不写", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(editUrl)
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  await page.getByLabel("Campaign 日预算", { exact: true }).fill("100.0")
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
test("超时保存按 request_id 精确确认，不重复发送版本写入", async ({ page }) => {
  const { requests } = await boundary(page, { mode: "unknown" })
  await page.goto(editUrl)
  await page.getByLabel("Campaign 日预算", { exact: true }).fill("101.00")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  const writes = requests.filter(
    (r) => r.method === "POST" && !r.path.endsWith("/validate"),
  )
  expect(writes).toHaveLength(1)
  expect(
    requests.some((r) =>
      r.path.endsWith(`/strategy-save-requests/${writes[0].body.request_id}`),
    ),
  ).toBe(true)
  expect(new URL(page.url()).searchParams.get("version_id")).toBe(
    "77777777-7777-4777-8777-777777777777",
  )
})
test("未知保存回查404不视为未保存，刷新后仍只回查同一请求", async ({
  page,
}) => {
  const { requests } = await boundary(page, { mode: "unknown404" })
  await page.goto(editUrl)
  await page.getByLabel("Campaign 日预算", { exact: true }).fill("101.00")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("保存结果待确认", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  const write = requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/versions"),
  )!
  await page.getByRole("button", { name: "确认保存结果", exact: true }).click()
  await expect(page.getByText(/暂未确认这次保存记录/)).toBeVisible()
  page.once("dialog", (dialog) => dialog.accept())
  await page.reload()
  await expect(page.getByText("保存结果待确认", { exact: true })).toBeVisible()
  expect(
    requests.filter((r) => r.method === "POST" && r.path.endsWith("/versions")),
  ).toHaveLength(1)
  expect(
    requests
      .filter((r) => r.path.includes("/strategy-save-requests/"))
      .every((r) => r.path.endsWith(write.body.request_id)),
  ).toBe(true)
})
test("409保留输入并显示服务器差异；不会自动变更版本基线", async ({ page }) => {
  const { requests } = await boundary(page, { mode: "conflict" })
  await page.goto(editUrl)
  await page.getByLabel("Campaign 日预算", { exact: true }).fill("130.00")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(
    page.getByText("服务器当前为 v2，本地输入已保留。"),
  ).toBeVisible()
  await expect(page.getByLabel("Campaign 日预算", { exact: true })).toHaveValue(
    "130.00",
  )
  await page.getByRole("button", { name: "查看服务器差异" }).click()
  await expect(page.getByRole("dialog")).toContainText("服务器：120.00")
  await expect(page.getByRole("dialog")).toContainText("本地：130.00")
  expect(
    requests.filter((r) => r.method === "POST" && r.path.endsWith("/versions")),
  ).toHaveLength(1)
})
test("98条有效文案阻止99创意，非法变量定位字段；保留已有CTA ID", async ({
  page,
}) => {
  const { requests } = await boundary(page, { capacity: 98 })
  await page.goto(editUrl)
  await page.getByLabel("创意数量", { exact: true }).fill("99")
  await expect(page.getByLabel("创意数量", { exact: true })).toHaveAttribute(
    "aria-invalid",
    "true",
  )
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  await page.getByLabel("创意数量", { exact: true }).fill("3")
  await page
    .getByLabel("Campaign 后缀模板", { exact: true })
    .fill("-{bad}-{batch_short_id}")
  await expect(
    page.getByLabel("Campaign 后缀模板", { exact: true }),
  ).toHaveAttribute("aria-invalid", "true")
  await expect(
    page.getByText("已有 ID：official-existing-id", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("CTA 候选尚未就绪，待场景核实。", { exact: true }),
  ).toBeVisible()
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
test("预算大数十进制原文保存，结构和命名示例不进入配置", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(editUrl)
  await page
    .getByLabel("Campaign 日预算", { exact: true })
    .fill("9007199254740993.12")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  const write = requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/versions"),
  )!
  expect(write.body.config.budget).toBe("9007199254740993.12")
  expect(write.body.config.target_roas).toBe("1.08")
  expect(write.body.config.cta_option_ids).toEqual(["official-existing-id"])
  expect(Object.keys(write.body.config).sort()).toEqual(
    Object.keys(config).sort(),
  )
  expect(write.body.expected_version).toBe(1)
})
test("策略205条列表默认可用、50/100服务端游标与字段对应", async ({ page }) => {
  const { requests } = await boundary(page, { count: 205 })
  await page.goto(`/tenants/${A}/strategies`)
  await expect(page.locator("tbody tr")).toHaveCount(50)
  const first = page.locator("tbody tr").first()
  await expect(first).toContainText("USD 100.00")
  await expect(first).toContainText("1.08 倍")
  await expect(first).toContainText("10 条/组")
  await expect(first).toContainText("SP1～SP2")
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(
    page.getByRole("link", { name: "分页策略 51", exact: true }),
  ).toBeVisible()
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(100)
  expect(
    requests
      .filter((r) => r.path.endsWith("/strategies"))
      .map((r) => [
        r.query.get("limit"),
        r.query.get("cursor"),
        r.query.get("active"),
      ]),
  ).toEqual([
    ["50", null, "true"],
    ["50", "50", "true"],
    ["100", null, "true"],
  ])
  await page.getByLabel("策略名称", { exact: true }).fill("%_")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(
    page.getByText("没有符合条件的记录", { exact: true }),
  ).toBeVisible()
  expect(
    requests
      .filter((r) => r.path.endsWith("/strategies"))
      .slice(-1)[0]
      .query.get("query"),
  ).toBe("%_")
})
test("只读成员能看版本和文案池，不能编辑复制或停用", async ({ page }) => {
  const { requests } = await boundary(page, { role: "viewer" })
  await page.goto(`/tenants/${A}/strategies`)
  await expect(
    page.getByRole("link", { name: "新建策略", exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: /^编辑$|复制为新策略|^停用$/ }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "查看版本", exact: true }).click()
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "查看版本", exact: true })
    .click()
  await expect(
    page.getByLabel("Campaign 日预算", { exact: true }),
  ).toHaveAttribute("readonly", "")
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "查看文案", exact: true }).click()
  const sheet = page.getByRole("dialog")
  await expect(
    sheet.getByText("当前有效去重正文：100 条 · 英文", { exact: true }),
  ).toBeVisible()
  await expect(sheet.locator("li")).toHaveCount(100)
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})
test("新建空表单无生产预算默认值，空租户和403明确区分", async ({ page }) => {
  await boundary(page, { empty: true })
  await page.goto(`/tenants/${A}/strategies`)
  await expect(page.getByText("还没有投放策略", { exact: true })).toBeVisible()
  await page.getByRole("link", { name: "新建策略", exact: true }).click()
  await expect(page.getByLabel("Campaign 日预算", { exact: true })).toHaveValue(
    "",
  )
  await expect(page.getByLabel("创意数量", { exact: true })).toHaveValue("")
  await expect(
    page.getByRole("button", { name: "创建策略", exact: true }),
  ).toBeDisabled()
})
test("策略停用只PATCH本地可用性，旧版本保留且可恢复", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${A}/strategies`)
  await page.getByRole("button", { name: "停用", exact: true }).click()
  await expect(page.getByRole("dialog")).toContainText("已提交任务不受影响")
  await page.getByRole("button", { name: "确认", exact: true }).click()
  await expect(page.getByRole("dialog")).toHaveCount(0)
  await page.getByRole("combobox", { name: "策略状态" }).click()
  await page.getByRole("option", { name: "已停用", exact: true }).click()
  await page.getByRole("link", { name: "租户策略", exact: true }).click()
  await expect(page.getByText("策略已停用", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toHaveCount(0)
  expect(
    requests
      .filter((r) => r.method !== "GET")
      .map((r) => [r.method, r.path, r.body]),
  ).toEqual([["PATCH", `/api/tenants/${A}/strategies/${S}`, { active: false }]])
})
test("确定校验失败保留输入并标记字段，不进入未知回查", async ({ page }) => {
  const { requests } = await boundary(page, { mode: "failure" })
  await page.goto(editUrl)
  await page.getByLabel("创意数量", { exact: true }).fill("3")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByLabel("创意数量", { exact: true })).toHaveValue("3")
  await expect(page.getByLabel("创意数量", { exact: true })).toHaveAttribute(
    "aria-invalid",
    "true",
  )
  expect(
    requests.filter((r) => r.path.includes("/strategy-save-requests/")),
  ).toHaveLength(0)
})
test("命名只改后缀且三级同步；转义括号不冒充批次变量", async ({ page }) => {
  await boundary(page)
  await page.goto(editUrl)
  await page
    .getByLabel("Campaign 后缀模板", { exact: true })
    .fill("-后缀-{batch_short_id}")
  const naming = page.getByRole("region", { name: "广告命名示例" })
  await expect(naming.locator("dd")).toHaveText([
    "{b30008/s328302/c3}-The Bond-后缀-B7K2M9Q4",
    "{b30008/s328302/c3}-The Bond-后缀-B7K2M9Q4-g01",
    "{b30008/s328302/c3}-The Bond-后缀-B7K2M9Q4-g01-sp1",
  ])
  await expect(page.getByLabel("版权方归因基础名")).toHaveCount(0)
  await page
    .getByLabel("Campaign 后缀模板", { exact: true })
    .fill("-{{batch_short_id}}")
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  await expect(
    page.getByLabel("Campaign 后缀模板", { exact: true }),
  ).toHaveAttribute("aria-invalid", "true")
})
test("切租户先保护策略输入，确认后到新租户列表而非旧策略详情", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(editUrl)
  await page.getByLabel("Campaign 日预算", { exact: true }).fill("321.00")
  await page.getByRole("combobox", { name: "当前租户" }).click()
  await page.getByRole("option", { name: /策略租户乙/ }).click()
  await page.getByRole("button", { name: "留在当前页", exact: true }).click()
  await expect(page.getByLabel("Campaign 日预算", { exact: true })).toHaveValue(
    "321.00",
  )
  expect(new URL(page.url()).pathname).toContain(`/tenants/${A}/`)
  await page.getByRole("combobox", { name: "当前租户" }).click()
  await page.getByRole("option", { name: /策略租户乙/ }).click()
  await page
    .getByRole("button", { name: "丢弃未保存修改", exact: true })
    .click()
  await expect(page).toHaveURL(new RegExp(`/tenants/${B}/strategies/?$`))
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
  expect(
    requests.some((r) => r.path === `/api/tenants/${B}/strategies/${S}`),
  ).toBe(false)
})
for (const path of [`/tenants/${A}/strategies/new`, editUrl])
  test(`登录恢复合法策略路由 ${path}`, async ({ page }) => {
    await boundary(page, { loggedOut: true })
    await page.goto(path)
    await expect(page).toHaveURL(/\/login$/)
    await page.getByLabel("邮箱", { exact: true }).fill("operator@example.com")
    await page.getByLabel("密码", { exact: true }).fill("synthetic-password")
    await page.getByRole("button", { name: "登录工作台", exact: true }).click()
    await expect(page).toHaveURL(path)
    await expect(
      page.getByLabel("Campaign 日预算", { exact: true }),
    ).toBeVisible()
  })
test("策略403保留登录而不展示空列表", async ({ page }) => {
  await boundary(page, { deny: true })
  await page.goto(`/tenants/${A}/strategies`)
  await expect(page.getByText("无权访问此页面", { exact: true })).toBeVisible()
  await expect(page.getByText("还没有投放策略", { exact: true })).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "strategy-test-token",
  )
})
test("历史版本始终只读，当前版本更新不改变旧版本预算", async ({ page }) => {
  const { records, versions } = await boundary(page)
  records[0].latest_version = 2
  records[0].version_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  records[0].config = { ...config, budget: "180.00" }
  versions.push({
    ...versions[0],
    id: records[0].version_id,
    number: 2,
    config: records[0].config,
  })
  await page.goto(`${editUrl}?version_id=${V}`)
  await expect(page.getByLabel("Campaign 日预算", { exact: true })).toHaveValue(
    "100.00",
  )
  await expect(
    page.getByLabel("Campaign 日预算", { exact: true }),
  ).toHaveAttribute("readonly", "")
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toHaveCount(0)
})
test("精确回查返回本次v2，即使服务器最新v3也不冒认其他版本", async ({
  page,
}) => {
  const { requests, records, versions } = await boundary(page, {
    mode: "unknown",
  })
  await page.route(
    "**/api/tenants/*/strategy-save-requests/*",
    async (route) => {
      records[0].latest_version = 3
      records[0].version_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
      records[0].config = { ...config, budget: "999.00" }
      if (!versions.some((v) => v.number === 3))
        versions.push({
          ...versions[0],
          id: records[0].version_id,
          number: 3,
          config: records[0].config,
        })
      await route.fallback()
    },
  )
  await page.goto(editUrl)
  await page.getByLabel("Campaign 日预算", { exact: true }).fill("101.00")
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click()
  await expect(page.getByText("已保存 v2", { exact: true })).toBeVisible()
  await expect(page.getByLabel("Campaign 日预算", { exact: true })).toHaveValue(
    "101.00",
  )
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toHaveCount(0)
  expect(
    requests.filter((r) => r.method === "POST" && r.path.endsWith("/versions")),
  ).toHaveLength(1)
})
test("延迟策略查询在切租户时取消，不把旧详情读入新租户", async ({ page }) => {
  const { requests } = await boundary(page)
  let release!: () => void, started!: () => void
  const hold = new Promise<void>((r) => {
      release = r
    }),
    start = new Promise<void>((r) => {
      started = r
    }),
    failed: string[] = []
  page.on("requestfailed", (r) => failed.push(r.url()))
  await page.route(`**/api/tenants/${A}/strategies/${S}`, async (route) => {
    started()
    await hold
    await route.fulfill({ json: original }).catch(() => {})
  })
  await page.goto(editUrl)
  await start
  await page.getByRole("combobox", { name: "当前租户" }).click()
  await page.getByRole("option", { name: /策略租户乙/ }).click()
  await expect(page).toHaveURL(new RegExp(`/tenants/${B}/strategies/?$`))
  release()
  await expect
    .poll(() =>
      failed.some((url) => url.includes(`/tenants/${A}/strategies/${S}`)),
    )
    .toBe(true)
  expect(
    requests.some((r) => r.path === `/api/tenants/${B}/strategies/${S}`),
  ).toBe(false)
})
test("切换BC保留当前租户策略，目录请求没有BC筛选", async ({ page }) => {
  const { requests } = await boundary(page)
  const BC1 = "7000000000000000001",
    BC2 = "7000000000000000002"
  await page.route("**/api/tenants/*/bcs*", (route) =>
    route.fulfill({
      json: {
        items: [
          { bc_id: BC1, name: "第一BC" },
          { bc_id: BC2, name: "第二BC" },
        ],
        next_cursor: null,
      },
    }),
  )
  await page.goto(`${editUrl}?bc_id=${BC1}`)
  await expect(page.getByLabel("Campaign 日预算", { exact: true })).toHaveValue(
    "100.00",
  )
  await page.getByRole("combobox", { name: "当前 BC" }).click()
  await page.getByRole("option", { name: /第二BC/ }).click()
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC2}`))
  await expect(page.getByLabel("Campaign 日预算", { exact: true })).toHaveValue(
    "100.00",
  )
  expect(
    requests
      .filter((r) => r.path.includes("/strategies"))
      .every((r) => !r.query.has("bc_id")),
  ).toBe(true)
})
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 900, height: 900 },
  { width: 390, height: 844 },
])
  test(`策略编辑${viewport.width}px布局与固定操作区`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport)
    await boundary(page)
    await page.goto(editUrl)
    await expect(
      page.getByLabel("Campaign 日预算", { exact: true }),
    ).toHaveValue("100.00")
    await expect(
      page.getByRole("region", { name: "结构与预算示例" }),
    ).toBeVisible()
    await page.getByLabel("创意数量", { exact: true }).fill("3")
    await page
      .getByRole("region", { name: "结构与预算示例" })
      .scrollIntoViewIfNeeded()
    const size = await page.evaluate(() => ({
      viewport: innerWidth,
      body: document.documentElement.scrollWidth,
    }))
    expect(size.body).toBeLessThanOrEqual(size.viewport)
    await page
      .getByRole("button", { name: "保存为新版本", exact: true })
      .scrollIntoViewIfNeeded()
    await expect(
      page.getByRole("button", { name: "保存为新版本", exact: true }),
    ).toBeInViewport()
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({
      path: testInfo.outputPath(`strategy-${viewport.width}.png`),
      fullPage: false,
    })
  })
test("后台检查版本变化保留本地配置，临时读取失败也不卸载表单", async ({
  page,
}) => {
  const { records } = await boundary(page)
  await page.goto(editUrl)
  await page.getByLabel("Campaign 日预算", { exact: true }).fill("130.00")
  records[0].latest_version = 2
  records[0].config = {
    ...config,
    budget: "120.00",
    cta_option_ids: ["other-existing-id"],
  }
  await page.getByRole("button", { name: "检查最新版本", exact: true }).click()
  await expect(
    page.getByText("服务器当前为 v2，本地输入已保留。"),
  ).toBeVisible()
  await expect(page.getByLabel("Campaign 日预算", { exact: true })).toHaveValue(
    "130.00",
  )
  await expect(
    page.getByText("已有 ID：official-existing-id", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toBeDisabled()
  await page.route(`**/api/tenants/${A}/strategies/${S}`, (route) =>
    route.fulfill({ status: 500, json: { message: "读取暂时失败" } }),
  )
  await page.getByRole("button", { name: "检查最新版本", exact: true }).click()
  await expect(page.getByText("读取暂时失败", { exact: true })).toBeVisible({
    timeout: 10000,
  })
  await expect(page.getByLabel("Campaign 日预算", { exact: true })).toHaveValue(
    "130.00",
  )
})
