import { expect, test } from "@playwright/test"

test.use({ storageState: { cookies: [], origins: [] } })
test("forgotten password directs to platform administrator without collecting identity", async ({
  page,
}) => {
  await page.goto("/recover-password")
  await expect(
    page.getByText("联系管理员重置密码", { exact: true }),
  ).toBeVisible()
  await expect(page.locator("input")).toHaveCount(0)
  await page.getByRole("link", { name: "返回登录" }).click()
  await expect(page).toHaveURL("/login")
  await page.goto("/reset-password?token=obsolete-synthetic-token")
  await expect(page).toHaveURL("/recover-password")
})
