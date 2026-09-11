import { expect, test } from "@playwright/test"
import {
  BC,
  buildsBoundary,
  D,
  P,
  pickInputs,
  S,
  T,
  T2,
} from "./utils/buildsBoundary"
import { expectWorkspaceLayout } from "./utils/workspaceLayout"

test("BC 已切换默认连接时旧预览仍展示原连接", async ({ page }) => {
  const api = await buildsBoundary(page, {
    defaultConnectionId: S,
    channel: "OFFICIAL_API",
  })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page
      .getByRole("region", { name: "执行连接" })
      .getByText("原搭建连接", { exact: true }),
  ).toBeVisible()
  await expect(
    page
      .getByRole("region", { name: "执行连接" })
      .getByText("官方 API", { exact: true }),
  ).toBeVisible()
  expect(api.requests.some((r) => r.path.endsWith("/bcs"))).toBe(true)
})

test("切换租户清除旧预览连接且不读取新租户下的旧预览", async ({ page }) => {
  const api = await buildsBoundary(page, { channel: "OFFICIAL_MCP" })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(page.getByText("原搭建连接", { exact: true })).toBeVisible()
  await page.getByRole("combobox", { name: "当前租户", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "搭建租户乙" }).click()
  await expect(page).toHaveURL(new RegExp(`/tenants/${T2}/builds/new`))
  await expect(page.getByText("原搭建连接", { exact: true })).toHaveCount(0)
  expect(
    api.requests.some((r) =>
      r.path.includes(`/tenants/${T2}/build-previews/${P}`),
    ),
  ).toBe(false)
})

test("只读角色看到未知结果时没有新授权核查或重试创建", async ({ page }) => {
  const api = await buildsBoundary(page, { viewer: true, historicalRead: true })
  await page.goto(
    `/tenants/${T}/build-tasks/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb?bc_id=${BC}&tab=issues`,
  )
  await page.getByRole("button", { name: "查看原因", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "按新授权只读核查", exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: /重试失败|重试创建/ }),
  ).toHaveCount(0)
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(0)
})

test("新授权只读核查保留原任务并展示独立结果", async ({ page }) => {
  const api = await buildsBoundary(page, {
    admin: true,
    historicalRead: true,
    channel: "OFFICIAL_MCP",
  })
  await page.goto(
    `/tenants/${T}/build-tasks/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb?bc_id=${BC}&tab=issues`,
  )
  await page.getByRole("button", { name: "查看原因", exact: true }).click()
  await expect(
    page.getByText("本次只读取原对象，不会继续创建广告。", { exact: true }),
  ).toBeVisible()
  await page
    .getByRole("button", { name: "按新授权只读核查", exact: true })
    .click()
  await expect(page.getByText("已核实原对象", { exact: true })).toBeVisible()
  await expect(
    page.getByText("existing-remote-id", { exact: true }),
  ).toBeVisible()
  const posts = api.requests.filter((r) => r.method === "POST")
  expect(posts).toHaveLength(1)
  expect(posts[0]!.path).toContain("/historical-read")
  expect(Object.keys(posts[0]!.body)).toEqual(["request_id"])
})

test("新授权核查响应丢失只查原请求，404不重复安排", async ({ page }) => {
  const api = await buildsBoundary(page, {
    admin: true,
    historicalRead: true,
    historicalReadLost: true,
    lookup404: true,
  })
  const url = `/tenants/${T}/build-tasks/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb?bc_id=${BC}&tab=issues`
  await page.goto(url)
  await page.getByRole("button", { name: "查看原因", exact: true }).click()
  await page
    .getByRole("button", { name: "按新授权只读核查", exact: true })
    .click()
  await page
    .getByRole("button", { name: "查询原核查请求", exact: true })
    .click()
  await expect(
    page.getByText("尚未查到原请求，请继续核实。", { exact: true }),
  ).toBeVisible()
  await page.reload()
  await page.getByRole("button", { name: "查看原因", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "按新授权只读核查", exact: true }),
  ).toHaveCount(0)
  await page
    .getByRole("button", { name: "查询原核查请求", exact: true })
    .click()
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(1)
})

for (const state of ["UNKNOWN", "BLOCKED"] as const) {
  test(`已明确${state}的核查可由管理员再次显式只读核查`, async ({ page }) => {
    const api = await buildsBoundary(page, {
      admin: true,
      historicalRead: true,
      historicalReadState: state,
    })
    await page.goto(
      `/tenants/${T}/build-tasks/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb?bc_id=${BC}&tab=issues`,
    )
    await page.getByRole("button", { name: "查看原因", exact: true }).click()
    await page
      .getByRole("button", { name: "按新授权只读核查", exact: true })
      .click()
    const restart = page.getByRole("button", {
      name: "再次只读核查",
      exact: true,
    })
    await expect(restart).toBeVisible()
    expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(1)
    await restart.click()
    await expect(page.getByText("已核实原对象", { exact: true })).toBeVisible()
    const posts = api.requests.filter((r) => r.method === "POST")
    expect(posts).toHaveLength(2)
    expect(posts.every((r) => r.path.endsWith("/historical-read"))).toBe(true)
    expect(posts[1]!.body.request_id).not.toBe(posts[0]!.body.request_id)
    await expect(restart).toHaveCount(0)
  })
}

test("仍在运行的独立核查不能再次排队", async ({ page }) => {
  const api = await buildsBoundary(page, {
    admin: true,
    historicalRead: true,
    historicalReadState: "RUNNING",
  })
  await page.goto(
    `/tenants/${T}/build-tasks/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb?bc_id=${BC}&tab=issues`,
  )
  await page.getByRole("button", { name: "查看原因", exact: true }).click()
  await page
    .getByRole("button", { name: "按新授权只读核查", exact: true })
    .click()
  await expect(
    page.getByText("已受理，正在只读核查", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "再次只读核查", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "刷新核查结果", exact: true }).click()
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(1)
})

test("新草稿可选择当前 BC 的 MCP 连接并随原输入保存", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${T}/builds/new?bc_id=${BC}`)
  await pickInputs(page)
  await page.getByRole("combobox", { name: "执行连接", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "MCP 搭建连接" }).click()
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect
    .poll(
      () =>
        api.requests.find(
          (r) => r.method === "POST" && r.path.endsWith("/build-drafts"),
        )?.body.execution_connection_id,
    )
    .toBe(S)
  expect(
    api.requests.filter(
      (r) => r.path.endsWith("/connections") && !r.path.includes("/providers/"),
    ),
  ).toHaveLength(1)
})

test("编辑草稿可清除指定连接并明确发送默认偏好", async ({ page }) => {
  const api = await buildsBoundary(page, { executionConnectionId: S })
  await page.goto(`/tenants/${T}/build-drafts/${D}?bc_id=${BC}&edit=true`)
  await expect(
    page.getByRole("combobox", { name: "执行连接", exact: true }),
  ).toContainText(S)
  await page
    .getByRole("button", { name: "使用 BC 默认连接", exact: true })
    .click()
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect
    .poll(
      () =>
        api.requests.find((r) => r.method === "PATCH")?.body
          .execution_connection_id,
    )
    .toBe(null)
})

for (const [channel, label] of [
  ["OFFICIAL_API", "官方 API"],
  ["OFFICIAL_MCP", "官方 MCP"],
] as const) {
  test(`冻结预览展示${label}连接并保留直接启用确认`, async ({ page }) => {
    await buildsBoundary(page, { channel })
    await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
    const route = page.getByRole("region", { name: "执行连接" })
    await expect(route.getByText(label, { exact: true })).toBeVisible()
    await expect(route.getByText("原搭建连接", { exact: true })).toBeVisible()
    await expect(
      page.getByRole("button", { name: /创建并立即启用/ }),
    ).toBeEnabled()
  })
}

test("缺少历史执行连接的预览保留原内容并禁止创建", async ({ page }) => {
  const api = await buildsBoundary(page, { missingRoute: true })
  await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
  await expect(
    page.getByText("历史任务缺少可核实的执行连接，请重新准备。", {
      exact: true,
    }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: /创建并立即启用/ }),
  ).toBeDisabled()
  await expect(
    page.getByText("完整剧名1", { exact: true }).first(),
  ).toBeVisible()
  expect(api.requests.filter((r) => r.method === "POST")).toEqual([])
})

for (const width of [390, 900, 1440]) {
  test(`执行连接在${width}px完整展示且没有横向溢出`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await buildsBoundary(page, { channel: "OFFICIAL_MCP" })
    await page.goto(`/tenants/${T}/build-previews/${P}?bc_id=${BC}`)
    await expect(page.getByRole("region", { name: "执行连接" })).toBeVisible()
    await expectWorkspaceLayout(page)
  })
}
