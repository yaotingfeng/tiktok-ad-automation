import { expect, test } from "@playwright/test"
import { automationBoundary, TENANT } from "./utils/automationBoundary"

test("规则列表显示摘要与模式", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await expect(page.getByRole("heading", { name: "自动规则" })).toBeVisible()
  const row = page.getByRole("row", { name: /剧级止损/ })
  await expect(row).toContainText("每部剧合计 · 今天消耗 > 100")
  await expect(row).toContainText("仅通知")
})

test("新建规则:联动选项并提交正确的定义", async ({ page }) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("textbox", { name: "规则名称" }).fill("广告组止损")
  await page.getByRole("radio", { name: "每部剧合计" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("100")
  await page.getByRole("button", { name: "添加条件" }).click()
  await page.getByRole("combobox", { name: "条件 2 指标" }).click()
  await page.getByRole("option", { name: "广告收益 ROAS" }).click()
  await page.getByRole("combobox", { name: "条件 2 运算符" }).click()
  await page.getByRole("option", { name: "小于" }).click()
  await page.getByRole("spinbutton", { name: "条件 2 数值" }).fill("0.8")
  await expect(page.getByRole("radio", { name: "自动执行" })).toBeDisabled()
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => created.length).toBe(1)
  expect(created[0]).toMatchObject({
    name: "广告组止损",
    definition: {
      aggregation: "DRAMA",
      mode: "NOTIFY",
      conditions: [
        { metric: "spend", range: "TODAY", operator: "GT", values: ["100"] },
        {
          metric: "roas_total",
          range: "TODAY",
          operator: "LT",
          values: ["0.8"],
        },
      ],
    },
  })
})

test("试运行展示命中与跳过账户", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
  await page.getByRole("button", { name: "试运行" }).click()
  await expect(page.getByText("判断 3 个对象，命中 1 个")).toBeVisible()
  await expect(
    page.getByRole("cell", { name: "总裁归来", exact: true }),
  ).toBeVisible()
  await expect(page.getByText("acc-9")).toBeVisible()
})

test("只读成员看不到写操作", async ({ page }) => {
  await automationBoundary(page, { role: "viewer" })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await expect(page.getByRole("row", { name: /剧级止损/ })).toBeVisible()
  await expect(page.getByRole("button", { name: "新建规则" })).toHaveCount(0)
})
