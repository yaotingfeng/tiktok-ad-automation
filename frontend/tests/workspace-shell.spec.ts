import { expect, type Page, test } from "@playwright/test"

// Intercept only the external API boundary. Every test renders the real app/router.
// The token/user below are synthetic and never sent to a live server.
const user = {
  id: "00000000-0000-4000-8000-000000000001",
  email: "workspace-test@example.invalid",
  full_name: "测试管理员",
  is_active: true,
  is_superuser: true,
}
const token = "workspace-ui-test-token"
const navigation = [
  "广告搭建",
  "搭建任务",
  "素材库",
  "投放策略",
  "账户与授权",
  "版权方连接",
  "成员管理",
]

async function apiBoundary(
  page: Page,
  options: {
    authenticated?: boolean
    admin?: boolean
    meStatus?: number
    usersStatus?: number
    createStatus?: number
  } = {},
) {
  if (options.authenticated !== false) {
    await page.addInitScript(
      (value) => localStorage.setItem("access_token", value),
      token,
    )
  }
  await page.route("**/api/**", async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === "OPTIONS")
      return route.fulfill({
        status: 204,
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Headers": "*",
          "Access-Control-Allow-Methods": "*",
        },
      })
    const headers = { "Access-Control-Allow-Origin": "*" }
    if (path.endsWith("/login/access-token"))
      return route.fulfill({
        json: { access_token: token, token_type: "bearer" },
        headers,
      })
    if (path.endsWith("/users/me"))
      return route.fulfill({
        status: options.meStatus ?? 200,
        json: options.meStatus
          ? { detail: "Forbidden" }
          : { ...user, is_superuser: options.admin ?? true },
        headers,
      })
    if (path.endsWith("/users/") && request.method() === "POST")
      return route.fulfill({
        status: options.createStatus ?? 200,
        json: options.createStatus
          ? { detail: "Forbidden" }
          : { ...user, ...request.postDataJSON() },
        headers,
      })
    if (path.endsWith("/users/"))
      return route.fulfill({
        status: options.usersStatus ?? 200,
        json: options.usersStatus
          ? { detail: "Forbidden" }
          : { data: [user], count: 1 },
        headers,
      })
    return route.fulfill({
      status: 404,
      json: { detail: "Unexpected test API request" },
      headers,
    })
  })
}

async function expectNoOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true)
}

test("unauthenticated entry and obsolete signup lead to the Chinese login", async ({
  page,
}) => {
  await apiBoundary(page, { authenticated: false })
  await page.goto("/")
  await expect(page).toHaveURL("/login")
  await expect(page.getByRole("heading", { name: "登录工作台" })).toBeVisible()
  await expect(page.getByRole("link", { name: /注册|Sign up/i })).toHaveCount(0)
  await page.goto("/signup")
  await expect(page).toHaveURL("/login")
  await expect(page.locator('input[autocomplete="username"]')).toBeVisible()
  await page.getByRole("button", { name: "登录工作台" }).click()
  await expect(page.getByLabel("邮箱", { exact: true })).toHaveAttribute(
    "aria-invalid",
    "true",
  )
  await expect(page.getByText("请输入有效的邮箱地址")).toBeVisible()
})

test("login submits credentials then reads the authenticated user with a bearer token", async ({
  page,
}) => {
  await apiBoundary(page, { authenticated: false })
  await page.goto("/login")
  await page.getByLabel("邮箱", { exact: true }).fill(user.email)
  await page.getByLabel("密码", { exact: true }).fill("synthetic-password")
  const loginRequest = page.waitForRequest((request) =>
    request.url().includes("/login/access-token"),
  )
  const meRequest = page.waitForRequest((request) =>
    request.url().includes("/users/me"),
  )
  await page.getByRole("button", { name: "登录工作台" }).click()
  const login = await loginRequest
  expect(new URL(login.url()).pathname).toBe("/api/login/access-token")
  expect(new URLSearchParams(login.postData()!).get("username")).toBe(
    user.email,
  )
  const me = await meRequest
  expect(new URL(me.url()).pathname).toBe("/api/users/me")
  expect(me.headers().authorization).toBe(`Bearer ${token}`)
  await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
})

for (const width of [1440, 1280]) {
  test(`authenticated empty shell has ordered navigation and no demo data at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 })
    await apiBoundary(page)
    await page.goto("/")
    await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
    await expect(page.getByText("BC 未连接", { exact: true })).toBeVisible()
    const labels = await page
      .getByRole("navigation")
      .getByRole("link")
      .allTextContents()
    expect(labels).toEqual([...navigation, "平台管理"])
    await expect(page.getByRole("button", { name: "新建搭建" })).toBeDisabled()
    await expect(
      page.getByRole("link", { name: /Items|Dashboard|注册|Sign up/i }),
    ).toHaveCount(0)
    await expect(page.getByText(/Demo BC|账户 A|DEMO-|ROI/)).toHaveCount(0)
    await page.getByRole("link", { name: "素材库", exact: true }).click()
    await expect(
      page.getByRole("heading", { name: "素材库", exact: true }),
    ).toBeVisible()
    await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
    await expectNoOverflow(page)
    await page.screenshot({
      path: test.info().outputPath(`workspace-${width}.png`),
      fullPage: true,
    })
  })
}

test("mobile navigation sheet is labelled, keyboard accessible, and closes after navigation", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await apiBoundary(page)
  await page.goto("/")
  await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
  const trigger = page.getByRole("button", { name: "切换导航" })
  await trigger.focus()
  await page.keyboard.press("Enter")
  const dialog = page.getByRole("dialog", { name: "工作台导航" })
  await expect(dialog).toBeVisible()
  const accountLink = dialog.getByRole("link", { name: "账户与授权" })
  await accountLink.focus()
  await page.keyboard.press("Enter")
  await expect(dialog).not.toBeVisible()
  await expect(page.getByRole("heading", { name: "账户与授权" })).toBeVisible()
  await expectNoOverflow(page)
  await page.screenshot({
    path: test.info().outputPath("workspace-mobile.png"),
    fullPage: true,
  })
})

test("mobile login fits one column and supports keyboard submission", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await apiBoundary(page, { authenticated: false })
  await page.goto("/login")
  await page.getByLabel("邮箱", { exact: true }).fill(user.email)
  await page.getByLabel("密码", { exact: true }).fill("synthetic-password")
  await page.screenshot({
    path: test.info().outputPath("login-mobile.png"),
    fullPage: true,
  })
  await expectNoOverflow(page)
  await page.keyboard.press("Enter")
  await expect(page.getByText("尚未接入租户", { exact: true })).toBeVisible()
})

test("403 user query retains the shell and login token with readable permission feedback", async ({
  page,
}) => {
  await apiBoundary(page, { meStatus: 403 })
  await page.goto("/")
  await expect(page.getByRole("alert").getByText("无操作权限")).toBeVisible()
  await expect(
    page.getByText("你没有执行此操作的权限。请联系管理员检查角色和租户权限。"),
  ).toBeVisible()
  await expect(page).toHaveURL("/")
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
  await expect(page.getByRole("navigation", { name: "投放工作" })).toBeVisible()
})

test("403 list query stays inside authenticated platform management", async ({
  page,
}) => {
  await apiBoundary(page, { usersStatus: 403 })
  await page.goto("/admin")
  await expect(page.getByText("无法读取用户列表")).toBeVisible()
  await expect(
    page.getByRole("alert").getByText("无操作权限", { exact: true }),
  ).toBeVisible()
  await expect(page.getByTestId("user-menu")).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
})

test("admin creation remains available and a 403 mutation does not sign out", async ({
  page,
}) => {
  await apiBoundary(page, { createStatus: 403 })
  await page.goto("/admin")
  await page.getByRole("button", { name: "Add User" }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByLabel("Email").fill("new-user@example.invalid")
  await dialog.getByLabel("Set Password").fill("synthetic-password")
  await dialog.getByLabel("Confirm Password").fill("synthetic-password")
  await dialog.getByRole("button", { name: "Save", exact: true }).click()
  await expect(
    page
      .getByText("你没有执行此操作的权限。请联系管理员检查角色和租户权限。")
      .first(),
  ).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    token,
  )
  await expect(page).toHaveURL("/admin")
})

test("ordinary users cannot enter platform management", async ({ page }) => {
  await apiBoundary(page, { admin: false })
  await page.goto("/admin")
  await expect(
    page.getByText("仅平台管理员可管理用户。请联系管理员获取权限。"),
  ).toBeVisible()
  await expect(page.getByRole("button", { name: "Add User" })).toHaveCount(0)
  await expect(
    page.getByRole("link", { name: "平台管理", exact: true }),
  ).toHaveCount(0)
})

test("401 clears an expired session and returns to login", async ({ page }) => {
  // Set the fixture once, so redirects cannot re-inject an expired token.
  await apiBoundary(page, { authenticated: false, meStatus: 401 })
  await page.goto("/login")
  await page.evaluate(
    (value) => localStorage.setItem("access_token", value),
    token,
  )
  await page.goto("/")
  await expect(page).toHaveURL("/login")
  expect(
    await page.evaluate(() => localStorage.getItem("access_token")),
  ).toBeNull()
})

test("administrator can create an account through the retained authenticated form", async ({
  page,
}) => {
  await apiBoundary(page)
  await page.goto("/admin")
  await page.getByRole("button", { name: "Add User" }).click()
  const dialog = page.getByRole("dialog")
  await dialog.getByLabel("Email").fill("new-user@example.com")
  await dialog.getByLabel("Set Password").fill("synthetic-password")
  await dialog.getByLabel("Confirm Password").fill("synthetic-password")
  const created = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname.endsWith("/users/"),
  )
  await dialog.getByRole("button", { name: "Save", exact: true }).click()
  const request = await created
  expect(request.headers().authorization).toBe(`Bearer ${token}`)
  expect(request.postDataJSON()).toMatchObject({
    email: "new-user@example.com",
    password: "synthetic-password",
  })
  await expect(page.getByText("User created successfully")).toBeVisible()
  await expect(dialog).not.toBeVisible()
})

test("the removed Items example has no public route", async ({ page }) => {
  await apiBoundary(page)
  await page.goto("/items")
  await expect(page.getByTestId("not-found")).toBeVisible()
  await expect(page.getByRole("button", { name: "Add Item" })).toHaveCount(0)
})
