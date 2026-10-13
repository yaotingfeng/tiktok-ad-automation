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

test("执行记录下钻到命中明细", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules?tab=runs`)
  const row = page.getByRole("row", { name: /剧级止损/ })
  await expect(row).toContainText("命中 1")
  await expect(row).toContainText("已发送")
  await row.getByRole("button", { name: "查看明细" }).click()
  await expect(
    page.getByRole("cell", { name: "总裁归来", exact: true }),
  ).toBeVisible()
})

test("飞书卡片链接直接打开指定运行", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules?tab=runs&run=run-1`)
  await expect(
    page.getByRole("cell", { name: "总裁归来", exact: true }),
  ).toBeVisible()
})

test("通知设置:掩码显示、测试与保存", async ({ page }) => {
  const saved: unknown[] = []
  const tested: unknown[] = []
  await automationBoundary(page, { saved, tested })
  await page.goto(`/tenants/${TENANT}/notifications`)
  await expect(
    page.getByText("https://open.feishu.cn/open-apis/bot/v2/hook/…ab12"),
  ).toBeVisible()
  await page
    .getByRole("textbox", { name: "Webhook 地址" })
    .fill("https://open.feishu.cn/open-apis/bot/v2/hook/new-1")
  await page.getByRole("button", { name: "发送测试消息" }).click()
  await expect.poll(() => tested.length).toBe(1)
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => saved.length).toBe(1)
  expect(saved[0]).toMatchObject({
    webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/new-1",
  })
})

test("非管理员没有通知设置入口", async ({ page }) => {
  await automationBoundary(page, { role: "operator" })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await expect(page.getByRole("link", { name: "通知设置" })).toHaveCount(0)
})

test("剧名按行填写，标题里的逗号不拆分；按月日期可以连续输入", async ({
  page,
}) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
  await page
    .getByRole("textbox", { name: "剧名" })
    .fill("嘉书-闪婚后，傅先生马甲藏不住了\n嘉书-总裁归来")
  await page.getByRole("radio", { name: "自定义时段" }).click()
  await page.getByRole("combobox", { name: "时间段 1 频次" }).click()
  await page.getByRole("option", { name: "按月" }).click()
  const days = page.getByRole("textbox", { name: "时间段 1 日期" })
  await days.pressSequentially("1,15")
  await expect(days).toHaveValue("1,15")
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => created.length).toBe(1)
  expect(created[0]).toMatchObject({
    definition: {
      apply_to: {
        drama_names: ["嘉书-闪婚后，傅先生马甲藏不住了", "嘉书-总裁归来"],
      },
      schedule: { periods: [{ date_type: "MONTH", days: [1, 15] }] },
    },
  })
})
