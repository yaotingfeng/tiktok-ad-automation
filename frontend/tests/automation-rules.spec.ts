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
  await expect(row).toContainText("仅通知")
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

test("通知设置:保存冷却期与每日涨幅上限", async ({ page }) => {
  const saved: unknown[] = []
  await automationBoundary(page, { saved })
  await page.goto(`/tenants/${TENANT}/notifications`)
  await page
    .getByRole("spinbutton", { name: "反向操作冷却期（小时）" })
    .fill("12")
  await page
    .getByRole("spinbutton", { name: "每日累计涨幅上限（%）" })
    .fill("50")
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => saved.length).toBe(1)
  expect(saved[0]).toMatchObject({
    reverse_cooldown_hours: 12,
    daily_increase_limit_pct: 50,
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

test("操作员不能选自动执行，可以选确认后执行", async ({ page }) => {
  const created: unknown[] = []
  await automationBoundary(page, { role: "operator", created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
  await expect(page.getByRole("radio", { name: "自动执行" })).toBeDisabled()
  await expect(page.getByText("需要租户管理员")).toBeVisible()
  await page.getByRole("radio", { name: "确认后执行" }).click()
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => created.length).toBe(1)
  expect(created[0]).toMatchObject({ definition: { mode: "CONFIRM" } })
})

test("管理员可以选自动执行", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await expect(page.getByRole("radio", { name: "自动执行" })).toBeEnabled()
})

test("表单有错误时列出并不提交", async ({ page }) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("button", { name: "保存" }).click()
  await expect(page.getByText("条件 1 需要填写数值")).toBeVisible()
  await page.getByRole("button", { name: "试运行" }).click()
  expect(created).toEqual([])
})

test("待确认批次：默认不勾选已不满足的对象，确认请求只含所选", async ({
  page,
}) => {
  const confirmed: unknown[] = []
  await automationBoundary(page, { confirmed })
  await page.goto(
    `/tenants/${TENANT}/automation-rules?tab=confirmations&confirmation=conf-1`,
  )
  const stale = page.getByRole("row", { name: /闪婚/ })
  await expect(stale).toContainText("条件已不满足")
  await expect(stale.getByRole("checkbox")).not.toBeChecked()
  const fresh = page.getByRole("row", { name: /总裁归来/ })
  await expect(fresh.getByRole("checkbox")).toBeChecked()
  await expect(fresh).toContainText("120")
  await expect(fresh).toContainText("135")
  await page.getByRole("button", { name: "确认执行所选" }).click()
  await expect.poll(() => confirmed.length).toBe(1)
  expect(confirmed[0]).toEqual({ subject_keys: ["drama:嘉书-总裁归来:USD"] })
})

test("待确认列表与忽略批次", async ({ page }) => {
  const dismissed: string[] = []
  await automationBoundary(page, { dismissed })
  await page.goto(`/tenants/${TENANT}/automation-rules?tab=confirmations`)
  const row = page.getByRole("row", { name: /剧级止损/ })
  await expect(row).toContainText("待确认")
  await row.getByRole("button", { name: "处理" }).click()
  await page.getByRole("button", { name: "忽略本批" }).click()
  await expect.poll(() => dismissed.length).toBe(1)
})

test("执行记录显示执行结果并链接操作记录", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules?tab=runs`)
  const row = page.getByRole("row", { name: /自动加预算/ })
  await expect(row).toContainText("部分完成")
  await row.getByRole("button", { name: "查看明细" }).click()
  await expect(page.getByText("提交 2 个对象")).toBeVisible()
  await expect(page.getByText(/夜间暂停/)).toBeVisible()
  await expect(page.getByRole("cell", { name: "已提交" })).toBeVisible()
  await expect(page.getByRole("link", { name: "操作记录" })).toHaveAttribute(
    "href",
    new RegExp(`^/tenants/${TENANT}/ad-management-tasks/task-1\\?bc_id=`),
  )
})

test("从模板新建：预填定义并保存", async ({ page }) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "从模板新建" }).click()
  await page.getByRole("button", { name: /剧级止损/ }).click()
  await expect(page.getByRole("textbox", { name: "规则名称" })).toHaveValue(
    "剧级止损",
  )
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => created.length).toBe(1)
  expect(created[0]).toMatchObject({
    name: "剧级止损",
    definition: {
      aggregation: "DRAMA",
      conditions: [
        { metric: "spend", values: ["100"] },
        { metric: "roas_total", operator: "LT", values: ["0.8"] },
      ],
    },
  })
})

test("待确认批次：手动取消的勾选不因数据刷新而恢复", async ({ page }) => {
  const confirmed: unknown[] = []
  const detailRequests: string[] = []
  await automationBoundary(page, { confirmed, detailRequests })
  await page.goto(
    `/tenants/${TENANT}/automation-rules?tab=confirmations&confirmation=conf-1`,
  )
  const fresh = page.getByRole("row", { name: /总裁归来/ })
  await fresh.getByRole("checkbox").click()
  await expect(fresh.getByRole("checkbox")).not.toBeChecked()
  const before = detailRequests.length
  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"))
    window.dispatchEvent(new Event("visibilitychange"))
  })
  await page.waitForTimeout(500)
  // 明细不在窗口重新聚焦时刷新，或刷新后保留用户的选择。
  expect(detailRequests.length).toBeGreaterThan(before)
  await expect(fresh.getByRole("checkbox")).not.toBeChecked()
  await expect(
    page.getByRole("button", { name: "确认执行所选" }),
  ).toBeDisabled()
})

test("飞书通知可选固定时间汇总发送", async ({ page }) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
  await page.getByRole("radio", { name: "固定时间汇总" }).click()
  await page.getByLabel("汇总时刻 1", { exact: true }).fill("09:00")
  await page.getByRole("button", { name: "添加汇总时刻" }).click()
  await page.getByLabel("汇总时刻 2", { exact: true }).fill("18:30")
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => created.length).toBe(1)
  expect(created[0]).toMatchObject({
    definition: {
      notification: {
        timing: "SUMMARY",
        summary_times: ["09:00", "18:30"],
      },
    },
  })
})
