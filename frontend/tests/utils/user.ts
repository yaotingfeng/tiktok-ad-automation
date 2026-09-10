import { expect, type Page } from "@playwright/test"

export async function logInUser(
  page: Page,
  username: string,
  password: string,
) {
  await page.goto("/login")

  await page.getByTestId("username-input").fill(username)
  await page.getByTestId("password-input").fill(password)
  await page.getByRole("button", { name: "登录 TT ADA" }).click()
  await page.waitForURL((url) => url.pathname !== "/login")
  await expect(page.getByTestId("user-menu")).toBeVisible()
}

export async function logOutUser(page: Page) {
  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "退出登录" }).click()
  await page.goto("/login")
}
