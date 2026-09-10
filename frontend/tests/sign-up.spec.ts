import { expect, test } from "@playwright/test"

test.use({ storageState: { cookies: [], origins: [] } })

test("public registration redirects to login", async ({ page }) => {
  await page.goto("/signup")
  await expect(page).toHaveURL("/login")
  await expect(page.getByRole("button", { name: "登录 TT ADA" })).toBeVisible()
  await expect(page.getByRole("link", { name: /注册|Sign up/i })).toHaveCount(0)
})
