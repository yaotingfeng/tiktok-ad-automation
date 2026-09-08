import { expect, type Page } from "@playwright/test"

export async function logInUser(page: Page, email: string, password: string) {
  await page.goto("/login")

  await page.getByTestId("email-input").fill(email)
  await page.getByTestId("password-input").fill(password)
  await page.getByRole("button", { name: "登录工作台" }).click()
  await page.waitForURL("/")
  await expect(page.getByText("尚未接入租户")).toBeVisible()
}

export async function logOutUser(page: Page) {
  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "退出登录" }).click()
  await page.goto("/login")
}
