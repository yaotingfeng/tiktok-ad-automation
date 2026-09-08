import { expect, type Page, test } from "@playwright/test"

// Synthetic HTTP boundary fixtures; the real application, router, generated SDK,
// dialogs and queries render unmodified. No TikTok or live user data is involved.
const A = "11111111-1111-4111-8111-111111111111"
const B = "22222222-2222-4222-8222-222222222222"
const C = "33333333-3333-4333-8333-333333333333"
const U = "44444444-4444-4444-8444-444444444444"
const V = "55555555-5555-4555-8555-555555555555"
const token = "tenant-browser-test-token"
type Role = "platform_admin" | "tenant_admin" | "operator" | "viewer"
type Tenant = { id: string; name: string; active: boolean; role: Role }
type Member = {
  tenant_id: string
  user_id: string
  email: string
  full_name: string
  role: Exclude<Role, "platform_admin">
  active: boolean
  user_active: boolean
}
type Request = {
  method: string
  path: string
  query: URLSearchParams
  body: unknown
  authorization?: string
}
async function boundary(
  page: Page,
  options: {
    platform?: boolean
    role?: Role
    memberCount?: number
    count?: number
    lastAdmin?: boolean
    members403?: boolean
    delayedA?: Promise<void>
    listFailure?: boolean
  } = {},
) {
  const tenants: Tenant[] = [
    {
      id: A,
      name: "租户甲",
      active: true,
      role: options.role ?? "platform_admin",
    },
    {
      id: B,
      name: "租户乙",
      active: true,
      role: options.role ?? "platform_admin",
    },
    { id: C, name: "停用租户", active: false, role: "platform_admin" },
  ]
  if (options.count)
    for (let i = 4; i <= options.count; i++)
      tenants.push({
        id: `88888888-8888-4888-8888-${String(i).padStart(12, "0")}`,
        name: `分页租户 ${i}`,
        active: true,
        role: "platform_admin",
      })
  const members: Record<string, Member[]> = {
    [A]: [
      {
        tenant_id: A,
        user_id: U,
        email: "jia@example.com",
        full_name: "甲管理员",
        role: "tenant_admin",
        active: true,
        user_active: true,
      },
    ],
    [B]: [
      {
        tenant_id: B,
        user_id: V,
        email: "yi@example.com",
        full_name: "乙投手",
        role: "operator",
        active: true,
        user_active: true,
      },
    ],
  }
  if (options.memberCount)
    for (let i = 2; i <= options.memberCount; i++)
      members[A].push({
        tenant_id: A,
        user_id: `99999999-9999-4999-8999-${String(i).padStart(12, "0")}`,
        email: `member${i}@example.com`,
        full_name: `分页成员 ${i}`,
        role: "operator",
        active: true,
        user_active: true,
      })
  const requests: Request[] = []
  const candidates = [
    { id: U, email: "jia@example.com", full_name: "甲管理员" },
    { id: V, email: "candidate@example.com", full_name: "候选用户" },
  ]
  await page.addInitScript(
    (value) => localStorage.setItem("access_token", value),
    token,
  )
  await page.route("**/api/**", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const path = url.pathname
    const q = url.searchParams
    const headers = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "*",
      "Access-Control-Allow-Methods": "*",
    }
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers })
    const body = request.postData() ? request.postDataJSON() : undefined
    requests.push({
      method,
      path,
      query: q,
      body,
      authorization: request.headers().authorization,
    })
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ json, status, headers })
    const denied = () =>
      reply(
        {
          code: "action_forbidden",
          message: "当前角色不能执行此操作",
          retryable: false,
        },
        403,
      )
    const paginate = <T extends { id?: string; user_id?: string }>(
      rows: T[],
    ) => {
      const ordered = [...rows]
        .sort((a, b) => (a.id ?? a.user_id!).localeCompare(b.id ?? b.user_id!))
        .filter(
          (row) =>
            !q.get("after_id") || (row.id ?? row.user_id!) > q.get("after_id")!,
        )
      const size = Number(q.get("limit") ?? 50)
      const items = ordered.slice(0, size)
      return {
        items,
        next_cursor:
          ordered.length > size
            ? (items[items.length - 1]?.id ?? items[items.length - 1]?.user_id)
            : null,
      }
    }
    if (path === "/api/users/me")
      return reply({
        id: U,
        email: "admin@example.com",
        full_name: "当前用户",
        is_active: true,
        is_superuser: options.platform ?? true,
      })
    if (
      path === "/api/me/tenants" ||
      (path === "/api/platform/tenants" && method === "GET")
    ) {
      if (path.startsWith("/api/platform") && options.platform === false)
        return denied()
      if (options.listFailure && q.get("search") === "错误")
        return reply({ message: "暂时无法读取租户" }, 500)
      const search = q.get("search")?.toLowerCase() ?? ""
      return reply(
        paginate(
          tenants.filter(
            (tenant) =>
              (!q.has("active") ||
                tenant.active === (q.get("active") === "true")) &&
              (tenant.name.toLowerCase().includes(search) ||
                tenant.id === search),
          ),
        ),
      )
    }
    if (
      path.endsWith("user-candidates") ||
      path.endsWith("member-candidates")
    ) {
      expect(q.get("query")).toBeTruthy()
      return reply(
        paginate(
          candidates.filter((candidate) =>
            `${candidate.email}${candidate.full_name}`.includes(
              q.get("query") ?? "",
            ),
          ),
        ),
      )
    }
    if (path === "/api/platform/tenants" && method === "POST") {
      const created = {
        id: "77777777-7777-4777-8777-777777777777",
        name: body.name,
        active: true,
        role: "platform_admin" as const,
      }
      tenants.push(created)
      return reply(created, 201)
    }
    if (path.startsWith("/api/platform/tenants/") && method === "PATCH") {
      const tenant = tenants.find((row) => path.endsWith(row.id))!
      Object.assign(tenant, body)
      return reply(tenant)
    }
    const scope = /^\/api\/tenants\/([^/]+)\/members$/.exec(path)?.[1]
    if (scope && method === "GET") {
      if (
        options.members403 ||
        options.role === "viewer" ||
        options.role === "operator"
      )
        return denied()
      if (scope === A && options.delayedA) await options.delayedA
      const search = q.get("search")?.toLowerCase() ?? ""
      return reply(
        paginate(
          (members[scope] ?? []).filter(
            (row) =>
              (!q.has("role") || row.role === q.get("role")) &&
              (!q.has("active") ||
                row.active === (q.get("active") === "true")) &&
              `${row.email}${row.full_name}`.toLowerCase().includes(search),
          ),
        ),
      )
    }
    if (scope && method === "PUT") {
      if (options.lastAdmin)
        return reply(
          {
            code: "last_tenant_admin",
            message: "不能移除最后一位管理员",
            retryable: false,
          },
          409,
        )
      const candidate = candidates.find((row) => row.id === body.user_id)!
      const row = {
        tenant_id: scope,
        user_id: candidate.id,
        email: candidate.email,
        full_name: candidate.full_name,
        role: body.role,
        active: body.active,
        user_active: true,
      }
      members[scope] = [
        ...(members[scope] ?? []).filter(
          (item) => item.user_id !== row.user_id,
        ),
        row,
      ]
      return reply(row)
    }
    return reply({ message: "Unexpected API boundary request" }, 404)
  })
  return { requests, tenants, members }
}
async function chooseTenant(page: Page, name: string) {
  await page.getByRole("combobox", { name: "当前租户" }).click()
  const dialog = page.getByRole("dialog", { name: "选择当前租户" })
  await dialog.getByRole("option", { name: new RegExp(name) }).click()
}

test("platform login entry lists real tenant records and retains separate user provisioning", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto("/")
  await expect(page).toHaveURL("/platform/tenants")
  await expect(
    page.getByRole("heading", { name: "平台租户管理" }),
  ).toBeVisible()
  await expect(page.getByRole("row", { name: new RegExp(A) })).toBeVisible()
  await expect(page.getByRole("link", { name: "用户管理" })).toHaveAttribute(
    "href",
    "/admin",
  )
  await expect(page.getByText("BC 未连接", { exact: true })).toHaveCount(0)
  expect(
    requests.find((request) => request.path === "/api/platform/tenants")
      ?.authorization,
  ).toBe(`Bearer ${token}`)
})

test("platform tenant creation searches active user candidates and submits selected UUID", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto("/platform/tenants")
  await page.getByRole("button", { name: "新建租户" }).click()
  const sheet = page.getByRole("dialog", { name: "新建租户", exact: true })
  await sheet.getByRole("button", { name: "创建租户" }).click()
  await expect(sheet.getByText("请输入租户名称")).toBeVisible()
  await sheet.getByLabel("租户名称").fill("新租户")
  await sheet.getByRole("combobox", { name: "初始管理员" }).click()
  const picker = page.getByRole("dialog", { name: "选择初始管理员" })
  await picker.getByLabel("搜索初始管理员").fill("候选")
  await picker.getByRole("button", { name: "搜索", exact: true }).click()
  await picker.getByRole("option", { name: /候选用户/ }).click()
  await sheet.getByRole("button", { name: "创建租户" }).click()
  await expect(sheet).not.toBeVisible()
  await expect(page.getByRole("row", { name: /新租户/ })).toBeVisible()
  expect(requests.find((request) => request.method === "POST")?.body).toEqual({
    name: "新租户",
    administrator_id: V,
  })
  expect(
    requests.some((request) => /invite|\/users\/signup/.test(request.path)),
  ).toBe(false)
})

test("unsaved Sheet close can retain text or discard without sending any mutation", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto("/platform/tenants")
  await page.getByRole("button", { name: "新建租户" }).click()
  const sheet = page.getByRole("dialog", { name: "新建租户", exact: true })
  await sheet.getByLabel("租户名称").fill("未保存名称")
  await sheet.getByRole("button", { name: "取消", exact: true }).click()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(sheet.getByLabel("租户名称")).toHaveValue("未保存名称")
  await page.keyboard.press("Escape")
  await page.getByRole("button", { name: "丢弃未保存修改" }).click()
  await expect(sheet).not.toBeVisible()
  expect(requests.some((request) => request.method !== "GET")).toBe(false)
})

test("tenant edit, disable and reactivate use explicit writes; inactive details never fetch members", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto("/platform/tenants")
  const row = page.getByRole("row", { name: new RegExp(A) })
  await row.getByRole("button", { name: "编辑", exact: true }).click()
  await page
    .getByRole("dialog", { name: "编辑租户" })
    .getByLabel("租户名称")
    .fill("甲的新名称")
  await page.getByRole("button", { name: "保存修改" }).click()
  await expect(row).toContainText("甲的新名称")
  await row.getByRole("button", { name: "停用", exact: true }).click()
  await expect(
    page.getByText(/已在 TikTok 启用的广告不会自动停止/),
  ).toBeVisible()
  await page.getByRole("button", { name: "确认停用" }).click()
  await expect(row.getByRole("button", { name: "进入租户" })).toBeDisabled()
  const readsBefore = requests.filter((request) =>
    request.path.endsWith("/members"),
  ).length
  await row.getByRole("button", { name: "查看" }).click()
  await expect(page.getByText("恢复租户后可查看成员。")).toBeVisible()
  expect(
    requests.filter((request) => request.path.endsWith("/members")),
  ).toHaveLength(readsBefore)
  await page
    .getByRole("dialog", { name: "租户详情" })
    .getByRole("button", { name: "关闭", exact: true })
    .click()
  await row.getByRole("button", { name: "恢复", exact: true }).click()
  await page.getByRole("button", { name: "确认恢复" }).click()
  await expect(row.getByRole("button", { name: "进入租户" })).toBeEnabled()
  expect(
    requests
      .filter((request) => request.method === "PATCH")
      .map((request) => request.body),
  ).toEqual([{ name: "甲的新名称" }, { active: false }, { active: true }])
})

test("tenant list uses seek pagination at 50/100 and resets cursor on server filtering", async ({
  page,
}) => {
  const { requests } = await boundary(page, { count: 101 })
  await page.goto("/platform/tenants")
  await expect(page.locator("tbody tr")).toHaveCount(50)
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(page.getByText("第 2 页")).toBeVisible()
  await expect(page.locator("tbody tr")).toHaveCount(50)
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(page.getByText("第 1 页")).toBeVisible()
  await expect(page.locator("tbody tr")).toHaveCount(100)
  await page.getByLabel("搜索租户名称或 ID").fill("租户乙")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(1)
  const last = requests
    .filter((request) => request.path === "/api/platform/tenants")
    .slice(-1)[0]!
  expect(last.query.get("search")).toBe("租户乙")
  expect(last.query.has("after_id")).toBe(false)
  expect(last.query.get("limit")).toBe("100")
})

test("members can be added by tenant administrators through scoped candidate search", async ({
  page,
}) => {
  const { requests } = await boundary(page, {
    platform: false,
    role: "tenant_admin",
  })
  await page.goto(`/tenants/${A}/members`)
  await expect(page.getByRole("row", { name: /甲管理员/ })).toBeVisible()
  await page.getByRole("button", { name: "添加成员" }).click()
  const sheet = page.getByRole("dialog", { name: "添加成员", exact: true })
  await sheet.getByRole("combobox", { name: "已有用户" }).click()
  const picker = page.getByRole("dialog", { name: "选择已有用户" })
  await picker.getByLabel("搜索已有用户").fill("候选")
  await picker.getByRole("button", { name: "搜索", exact: true }).click()
  await picker.getByRole("option", { name: /候选用户/ }).click()
  await sheet.getByLabel("租户角色").click()
  await expect(page.getByRole("option")).toHaveCount(3)
  await page.getByRole("option", { name: "只读成员", exact: true }).click()
  await sheet.getByRole("button", { name: "保存成员" }).click()
  await expect(sheet).not.toBeVisible()
  await expect(page.getByRole("row", { name: /候选用户/ })).toContainText(
    "只读成员",
  )
  expect(requests.find((request) => request.method === "PUT")).toMatchObject({
    path: `/api/tenants/${A}/members`,
    body: { user_id: V, role: "viewer", active: true },
  })
  expect(requests.some((request) => request.path === "/api/users/")).toBe(false)
})

test("last administrator rejection stays inside the editor and preserves changed role", async ({
  page,
}) => {
  await boundary(page, { lastAdmin: true })
  await page.goto(`/tenants/${A}/members`)
  await page.getByRole("button", { name: "编辑成员" }).click()
  const sheet = page.getByRole("dialog", { name: "编辑成员" })
  await sheet.getByLabel("租户角色").click()
  await page.getByRole("option", { name: "只读成员", exact: true }).click()
  await sheet.getByRole("button", { name: "保存成员" }).click()
  await expect(
    sheet.getByText("请先设置其他租户管理员，再调整这位管理员。"),
  ).toBeVisible()
  await expect(sheet.getByLabel("租户角色")).toContainText("只读成员")
})

test("tenant switching changes URLs and clears old members while cancelling their request", async ({
  page,
}) => {
  let release!: () => void
  const delayedA = new Promise<void>((resolve) => {
    release = resolve
  })
  const { requests } = await boundary(page, { delayedA })
  await page.goto(`/tenants/${A}/members`)
  await expect(page.getByRole("combobox", { name: "当前租户" })).toContainText(
    "租户甲",
  )
  const cancelled = page.waitForEvent("requestfailed", {
    predicate: (request) => request.url().includes(`/tenants/${A}/members`),
  })
  await chooseTenant(page, "租户乙")
  await expect(page).toHaveURL(`/tenants/${B}/members`)
  await cancelled
  release()
  await expect(page.getByRole("row", { name: /乙投手/ })).toBeVisible()
  await expect(page.getByText("甲管理员", { exact: true })).toHaveCount(0)
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

test("browser navigation with an unsaved member form can cancel or discard without cross-tenant writes", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  await page.goto(`/tenants/${A}/members`)
  await chooseTenant(page, "租户乙")
  await page.getByRole("button", { name: "编辑成员" }).click()
  const sheet = page.getByRole("dialog", { name: "编辑成员" })
  await sheet.getByLabel("租户角色").click()
  await page.getByRole("option", { name: "只读成员", exact: true }).click()
  await page.goBack()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(page).toHaveURL(`/tenants/${B}/members`)
  await expect(sheet.getByLabel("租户角色")).toContainText("只读成员")
  await page.goBack()
  await page.getByRole("button", { name: "丢弃未保存修改" }).click()
  await expect(page).toHaveURL(`/tenants/${A}/members`)
  await expect(sheet).not.toBeVisible()
  await expect(page.getByRole("row", { name: /甲管理员/ })).toBeVisible()
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

for (const role of ["viewer", "operator"] as const)
  test(`${role} has no member write/navigation entry and keeps login on direct denial`, async ({
    page,
  }) => {
    await boundary(page, { platform: false, role })
    await page.goto(`/tenants/${A}/members`)
    await expect(
      page.getByText("无权访问此页面", { exact: true }),
    ).toBeVisible()
    await expect(
      page.getByRole("link", { name: "成员管理", exact: true }),
    ).toHaveCount(0)
    await expect(page.getByRole("button", { name: "添加成员" })).toHaveCount(0)
    expect(
      await page.evaluate(() => localStorage.getItem("access_token")),
    ).toBe(token)
    await page.getByRole("link", { name: "广告搭建", exact: true }).click()
    await expect(page).toHaveURL(`/tenants/${A}/builds/new`)
    await expect(page.getByText("尚未连接 TikTok BC")).toBeVisible()
    if (role === "viewer")
      await expect(page.getByRole("button", { name: "新建搭建" })).toHaveCount(
        0,
      )
  })

test("server permission revocation removes write actions while retaining the session", async ({
  page,
}) => {
  await boundary(page, { members403: true })
  await page.goto(`/tenants/${A}/members`)
  await expect(page.getByText("无权访问此页面", { exact: true })).toBeVisible()
  await expect(page.getByRole("button", { name: "添加成员" })).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
})

test("members pagination and role/status search are scoped server queries", async ({
  page,
}) => {
  const { requests } = await boundary(page, { memberCount: 101 })
  await page.goto(`/tenants/${A}/members`)
  await expect(page.locator("tbody tr")).toHaveCount(50)
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(page.getByText("第 2 页")).toBeVisible()
  await page.getByRole("button", { name: "上一页", exact: true }).click()
  await expect(page.getByRole("row", { name: /甲管理员/ })).toBeVisible()
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(100)
  await page.getByRole("combobox", { name: "角色筛选" }).click()
  await page.getByRole("option", { name: "租户管理员", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(1)
  await page.getByRole("combobox", { name: "状态筛选" }).click()
  await page.getByRole("option", { name: "正常", exact: true }).click()
  await page.getByLabel("搜索成员姓名或邮箱").fill("甲")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect
    .poll(() =>
      requests
        .filter((request) => request.path.endsWith("/members"))
        .slice(-1)[0]
        ?.query.get("search"),
    )
    .toBe("甲")
  const last = requests
    .filter((request) => request.path.endsWith("/members"))
    .slice(-1)[0]!
  expect(last.path).toBe(`/api/tenants/${A}/members`)
  expect(last.query.get("role")).toBe("tenant_admin")
  expect(last.query.get("active")).toBe("true")
  expect(last.query.get("limit")).toBe("100")
  expect(last.query.has("after_id")).toBe(false)
})

test("filter failures retain prior rows and remain distinct from zero matches", async ({
  page,
}) => {
  await boundary(page, { listFailure: true })
  await page.goto("/platform/tenants")
  await expect(page.getByRole("row", { name: new RegExp(A) })).toBeVisible()
  await page.getByLabel("搜索租户名称或 ID").fill("错误")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(page.getByText("暂时无法读取租户")).toBeVisible()
  await expect(page.getByRole("row", { name: new RegExp(A) })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "重试", exact: true }),
  ).toBeVisible()
  await page.getByLabel("搜索租户名称或 ID").fill("不匹配的租户")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await expect(
    page.getByText("没有符合条件的记录", { exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("row", { name: new RegExp(A) })).toHaveCount(0)
  await page.getByRole("button", { name: "清除筛选", exact: true }).click()
  await expect(page.getByRole("row", { name: new RegExp(A) })).toBeVisible()
})

for (const width of [1440, 390])
  test(`tenant platform and edit sheet remain readable at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 })
    await boundary(page)
    await page.goto("/platform/tenants")
    await expect(page.getByRole("row", { name: new RegExp(A) })).toBeVisible()
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    await page.screenshot({
      path: test.info().outputPath(`tenants-${width}.png`),
      fullPage: true,
      animations: "disabled",
    })
    await page.getByRole("button", { name: "新建租户" }).click()
    const sheet = page.getByRole("dialog", { name: "新建租户", exact: true })
    await expect(sheet.getByLabel("租户名称")).toBeFocused()
    await page.screenshot({
      path: test.info().outputPath(`tenant-sheet-${width}.png`),
      fullPage: true,
      animations: "disabled",
    })
    await page.keyboard.press("Escape")
    await expect(sheet).not.toBeVisible()
    await expect(page.getByRole("button", { name: "新建租户" })).toBeFocused()
  })

test("ordinary entry resolves an authorized tenant with scoped navigation and no invented BC", async ({
  page,
}) => {
  const { requests } = await boundary(page, {
    platform: false,
    role: "tenant_admin",
  })
  await page.goto("/")
  await expect(page).toHaveURL(`/tenants/${A}/builds/new`)
  await expect(page.getByRole("combobox", { name: "当前租户" })).toContainText(
    "租户甲",
  )
  await expect(
    page.getByRole("link", { name: "成员管理", exact: true }),
  ).toHaveAttribute("href", `/tenants/${A}/members`)
  await expect(
    page.getByRole("link", { name: "素材库", exact: true }),
  ).toHaveAttribute("href", `/tenants/${A}/materials`)
  await expect(page.getByText("BC 未连接", { exact: true })).toBeVisible()
  expect(
    requests.some((request) => /\/bcs|\/accounts/.test(request.path)),
  ).toBe(false)
})

test("an unavailable tenant URL never reuses another tenant's scope or member data", async ({
  page,
}) => {
  const { requests } = await boundary(page, {
    platform: false,
    role: "tenant_admin",
  })
  const unavailable = "00000000-0000-4000-8000-000000000000"
  await page.goto(`/tenants/${unavailable}/members`)
  await expect(page.getByText("无权访问此页面", { exact: true })).toBeVisible()
  await expect(page.getByText("甲管理员", { exact: true })).toHaveCount(0)
  expect(
    requests.some((request) =>
      request.path.includes(`/tenants/${unavailable}/members`),
    ),
  ).toBe(false)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
})
