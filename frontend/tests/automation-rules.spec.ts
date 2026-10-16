import type { Page } from "@playwright/test"
import { expect, test } from "@playwright/test"
import { automationBoundary, TENANT } from "./utils/automationBoundary"

async function chooseAction(page: Page, name: string) {
  await page.getByRole("combobox", { name: "操作" }).click()
  await page.getByRole("option", { name, exact: true }).click()
}

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
      // 跨账户合计只能按规则时区运行。
      schedule: { timezone_basis: "RULE" },
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

test("单对象规则默认按账户时区运行，合计口径改回规则时区", async ({ page }) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  const timezone = page.getByRole("radiogroup", { name: "运行时区" })
  await expect(
    timezone.getByRole("radio", { name: "按账户时区" }),
  ).toBeChecked()
  await page.getByRole("radio", { name: "每部剧合计" }).click()
  await expect(timezone).toHaveCount(0)
  await expect(page.getByText(/跨账户合计在 .+ 的同一时刻判断/)).toBeVisible()
  await page.getByRole("radio", { name: "每个对象" }).click()
  await expect(
    timezone.getByRole("radio", { name: "按规则时区" }),
  ).toBeChecked()
  await timezone.getByRole("radio", { name: "按账户时区" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => created.length).toBe(1)
  expect(created[0]).toMatchObject({
    definition: {
      aggregation: "EACH",
      schedule: { timezone_basis: "ACCOUNT" },
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

test("按月日期可以连续输入", async ({ page }) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
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
  await chooseAction(page, "暂停")
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
  await chooseAction(page, "暂停")
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
  await expect(
    page.getByRole("dialog").getByRole("link", { name: "操作记录" }),
  ).toHaveAttribute(
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
  await page.getByRole("radio", { name: "在特定时间统一发送通知" }).click()
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

test("规则概览、筛选与批量停用", async ({ page }) => {
  const batched: unknown[] = []
  await automationBoundary(page, { batched })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  const overview = page.getByRole("region", { name: "近 7 天概览" })
  await expect(overview).toContainText("执行动作20")
  await expect(overview).toContainText("待确认1")
  await expect(overview).toContainText("失败2")
  await expect(overview).toContainText("冲突4")
  await page.getByRole("combobox", { name: "按执行模式筛选" }).click()
  await page.getByRole("option", { name: "自动执行" }).click()
  await expect(page.getByRole("row", { name: /剧级止损/ })).toHaveCount(0)
  await expect(page.getByRole("row", { name: /跑量加预算/ })).toBeVisible()
  await page.getByRole("combobox", { name: "按执行模式筛选" }).click()
  await page.getByRole("option", { name: "全部模式" }).click()
  await page.getByRole("checkbox", { name: "选择 剧级止损" }).click()
  await page.getByRole("checkbox", { name: "选择 跑量加预算" }).click()
  await page.getByRole("button", { name: "批量停用" }).click()
  await expect.poll(() => batched.length).toBe(1)
  expect(batched[0]).toEqual({
    rule_ids: ["rule-1", "rule-3"],
    operation: "DISABLE",
  })
})

test("执行记录按结果、账户与剧名筛选", async ({ page }) => {
  const runQueries: string[] = []
  await automationBoundary(page, { runQueries })
  await page.goto(`/tenants/${TENANT}/automation-rules?tab=runs`)
  await page.getByRole("combobox", { name: "按结果筛选" }).click()
  await page.getByRole("option", { name: "有冲突" }).click()
  await page.getByRole("textbox", { name: "账户 ID" }).fill("acc-1")
  await page.getByRole("textbox", { name: "剧名" }).fill("总裁")
  await page.getByLabel("开始日期").fill("2026-10-13")
  await expect.poll(() => runQueries.at(-1) ?? "").toContain("result=CONFLICT")
  const last = new URLSearchParams(runQueries.at(-1))
  expect(last.get("advertiser_id")).toBe("acc-1")
  expect(last.get("drama")).toBe("总裁")
  expect(last.get("since")).toBe("2026-10-13")
})

test("搜索选择账户，在列表中勾选指定对象", async ({ page }) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
  await page.getByRole("radio", { name: "指定账户" }).click()
  await page.getByRole("searchbox", { name: "搜索账户" }).fill("美国")
  await page.getByRole("option", { name: /美国一号户/ }).click()
  await page.getByRole("button", { name: "批量粘贴账户 ID" }).click()
  await page
    .getByRole("textbox", { name: "账户 ID" })
    .fill("7600000000000000001\n7600000000000000009")
  await page.getByRole("radio", { name: "指定对象" }).click()
  // 选「指定对象」后直接列出对象，不需要先搜索；每行显示名称、账户、状态与 ID。
  const list = page.getByRole("table", { name: "对象列表" })
  const row = list.getByRole("row", { name: /组一/ })
  await expect(row).toContainText("7600000000000000001")
  await expect(row).toContainText("投放中")
  await row.getByRole("checkbox").click()
  await expect(page.getByText("已选 1 个")).toBeVisible()
  await page.getByRole("searchbox", { name: "搜索对象" }).fill("组二")
  await expect(list.getByRole("row", { name: /组一/ })).toHaveCount(0)
  await expect(page.getByText("已选 1 个")).toBeVisible()
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => created.length).toBe(1)
  expect(created[0]).toMatchObject({
    definition: {
      accounts: {
        mode: "ACCOUNTS",
        advertiser_ids: ["7600000000000000001", "7600000000000000009"],
      },
      apply_to: {
        status: "SELECTED",
        object_ids: ["7600000000000000001:g1"],
      },
    },
  })
})

test("对象列表可全选当前结果并加载更多", async ({ page }) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
  await page.getByRole("radio", { name: "指定对象" }).click()
  await page.getByRole("button", { name: "加载更多" }).click()
  await expect(
    page.getByRole("table", { name: "对象列表" }).getByRole("row", {
      name: /其他组51/,
    }),
  ).toBeVisible()
  await page.getByRole("checkbox", { name: "全选当前结果" }).click()
  await expect(page.getByText("已选 51 个")).toBeVisible()
  await page.getByRole("button", { name: "清空" }).click()
  await expect(page.getByText("已选 0 个")).toBeVisible()
})

test("应用范围没有剧名与名称过滤框，也没有单次执行对象上限", async ({
  page,
}) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await expect(page.getByRole("textbox", { name: "剧名" })).toHaveCount(0)
  await expect(page.getByRole("textbox", { name: "名称包含" })).toHaveCount(0)
  await expect(page.getByRole("textbox", { name: "名称不包含" })).toHaveCount(0)
  await expect(page.getByText("单次执行对象上限")).toHaveCount(0)
})

test("仅发送通知：没有执行层级与执行方式，通知固定开启", async ({ page }) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
  await expect(page.getByRole("combobox", { name: "操作" })).toHaveText(
    "仅发送通知",
  )
  await expect(page.getByRole("combobox", { name: "执行层级" })).toHaveCount(0)
  await expect(page.getByRole("radiogroup", { name: "执行方式" })).toHaveCount(
    0,
  )
  const notify = page.getByRole("checkbox", { name: "并发送通知" })
  await expect(notify).toBeChecked()
  await expect(notify).toBeDisabled()
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => created.length).toBe(1)
  expect(created[0]).toMatchObject({
    definition: { mode: "NOTIFY", notification: { enabled: true } },
  })
})

test("改广告的规则可以关闭通知；确认后执行关闭通知时提示去待确认页", async ({
  page,
}) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
  await chooseAction(page, "暂停")
  await expect(page.getByRole("radio", { name: "确认后执行" })).toBeChecked()
  const notify = page.getByRole("checkbox", { name: "并发送通知" })
  await notify.click()
  await expect(page.getByText(/请到「待确认」页处理/)).toBeVisible()
  await expect(page.getByRole("radiogroup", { name: "通知安排" })).toHaveCount(
    0,
  )
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => created.length).toBe(1)
  expect(created[0]).toMatchObject({
    definition: {
      mode: "CONFIRM",
      action: { type: "PAUSE" },
      notification: { enabled: false },
    },
  })
})

test("飞书通知文案与 TikTok 一致", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await expect(page.getByRole("radio", { name: "每次通知" })).toBeChecked()
  await expect(
    page.getByRole("radio", { name: "在特定时间统一发送通知" }),
  ).toBeVisible()
  await expect(
    page.getByRole("checkbox", { name: "不满足条件时也发送通知" }),
  ).toBeVisible()
})

test("调整数值显示单位", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await chooseAction(page, "调整预算")
  await expect(page.getByTestId("adjust-unit")).toHaveText("%")
  await page.getByRole("combobox", { name: "调整单位" }).click()
  await page.getByRole("option", { name: "金额" }).click()
  await expect(page.getByTestId("adjust-unit")).toHaveText("账户币种")
})

test.describe("浏览器在纽约时区", () => {
  test.use({ timezoneId: "America/New_York" })

  test("仅一次的运行时间按租户运行时区填写", async ({ page }) => {
    const created: unknown[] = []
    await automationBoundary(page, { created })
    await page.goto(`/tenants/${TENANT}/automation-rules`)
    await page.getByRole("button", { name: "新建规则" }).click()
    await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
    await page.getByRole("radio", { name: "仅一次" }).click()
    await expect(page.getByText("按 Asia/Shanghai 时间")).toBeVisible()
    await page.getByLabel("运行时间").fill("2030-01-02T09:30")
    await page.getByRole("button", { name: "保存" }).click()
    await expect.poll(() => created.length).toBe(1)
    // 上海 09:30 = UTC 01:30，与浏览器时区无关。
    expect(created[0]).toMatchObject({
      definition: { schedule: { once_at: "2030-01-02T01:30:00.000Z" } },
    })
  })
})
