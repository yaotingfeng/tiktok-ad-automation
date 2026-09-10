import { expect, type Page, test } from "@playwright/test"
import { firstSuperuser, firstSuperuserPassword } from "./config.ts"
import { randomPassword } from "./utils/random.ts"

test.use({ storageState: { cookies: [], origins: [] } })

const fillForm = async (page: Page, username: string, password: string) => {
  await page.getByTestId("username-input").fill(username)
  await page.getByTestId("password-input").fill(password)
}

const verifyInput = async (page: Page, testId: string) => {
  const input = page.getByTestId(testId)
  await expect(input).toBeVisible()
  await expect(input).toHaveText("")
  await expect(input).toBeEditable()
}

test("Inputs are visible, empty and editable", async ({ page }) => {
  await page.goto("/login")

  await verifyInput(page, "username-input")
  await verifyInput(page, "password-input")
})

test("Log In button is visible", async ({ page }) => {
  await page.goto("/login")

  await expect(page.getByRole("button", { name: "登录 TT ADA" })).toBeVisible()
})

test("Forgot Password link is visible", async ({ page }) => {
  await page.goto("/login")

  await expect(page.getByRole("link", { name: "忘记密码？" })).toBeVisible()
})

test("Log in with valid username and password ", async ({ page }) => {
  await page.goto("/login")

  await fillForm(page, firstSuperuser, firstSuperuserPassword)
  await page.getByRole("button", { name: "登录 TT ADA" }).click()

  await page.waitForURL("/platform/tenants")

  await expect(
    page.getByRole("heading", { name: "平台租户管理" }),
  ).toBeVisible()
})

test("Log in with invalid username", async ({ page }) => {
  await page.goto("/login")

  await fillForm(page, "legacy@example.com", firstSuperuserPassword)
  await page.getByRole("button", { name: "登录 TT ADA" }).click()

  await expect(
    page.getByText("账号需为 3–64 位字母、数字、下划线、点或短横线"),
  ).toBeVisible()
})

test("Log in with invalid password", async ({ page }) => {
  const password = randomPassword()

  await page.goto("/login")
  await fillForm(page, firstSuperuser, password)
  await page.getByRole("button", { name: "登录 TT ADA" }).click()

  await expect(
    page.getByText("登录失败，请检查账号和密码后重试。"),
  ).toBeVisible()
})

test("Successful log out", async ({ page }) => {
  await page.goto("/login")

  await fillForm(page, firstSuperuser, firstSuperuserPassword)
  await page.getByRole("button", { name: "登录 TT ADA" }).click()

  await page.waitForURL("/platform/tenants")

  await expect(
    page.getByRole("heading", { name: "平台租户管理" }),
  ).toBeVisible()

  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "退出登录" }).click()
  await page.waitForURL("/login")
})

test("Logged-out user cannot access protected routes", async ({ page }) => {
  await page.goto("/login")

  await fillForm(page, firstSuperuser, firstSuperuserPassword)
  await page.getByRole("button", { name: "登录 TT ADA" }).click()

  await page.waitForURL("/platform/tenants")

  await expect(
    page.getByRole("heading", { name: "平台租户管理" }),
  ).toBeVisible()

  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "退出登录" }).click()
  await page.waitForURL("/login")

  await page.goto("/settings")
  await page.waitForURL("/login")
})

test("Redirects to /login when token is wrong", async ({ page }) => {
  await page.goto("/settings")
  await page.evaluate(() => {
    localStorage.setItem("access_token", "invalid_token")
  })
  await page.goto("/settings")
  await page.waitForURL("/login")
  await expect(page).toHaveURL("/login")
})
