import { expect, test } from "@playwright/test"

const tenant = "11111111-1111-4111-8111-111111111111"
const optimizer = "22222222-2222-4222-8222-222222222222"
const sessionTenants = [
  tenant,
  "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
]

// 登录态与租户列表的 transport double；用例里后注册的路由优先于这里的兜底。
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("access_token", "ai-workbench-test-token"))
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === "/api/users/me")
      return route.fulfill({ json: { id: "44444444-4444-4444-8444-444444444444", username: "ai-test", full_name: "AI 测试", is_active: true, is_superuser: false } })
    if (path === "/api/me/tenants")
      return route.fulfill({ json: { items: sessionTenants.map((id, index) => ({ id, name: `租户 ${index + 1}`, active: true, role: "tenant_admin", default_bc_id: "bc-a" })), next_cursor: null, total: sessionTenants.length } })
    return route.fulfill({ json: { items: [], next_cursor: null, total: 0 } })
  })
})

test("AI 每日报告使用 transport double 展示冻结证据和待确认状态", async ({ page }) => {
  await page.route(`**/api/tenants/${tenant}/ai-optimizers`, (route) => route.fulfill({ json: { items: [{ id: optimizer, name: "自动优化师", active: true, version: 2, bc_id: "bc-a", advertiser_ids: ["acc-1"], strategy_version_id: "strategy-2", optimization_mode: "AUTO", rule_ids: [] }] } }))
  await page.route(`**/api/tenants/${tenant}/ai-optimizers/${optimizer}/daily-report**`, (route) => route.fulfill({ json: { business_date: "2026-11-12", optimization_mode: "AUTO", target_progress: { planned: 1, completed: 1, pending: 0, failed: 0, unknown: 0 }, schedule_rows: [{ id: "plan-1", title: "剧集 A", status: "COMPLETE", order_materials: true }], material: { requested: 1, not_requested: 0, pending: 0, matched: 1 }, rule_actions: [], pending_proposals: [{ id: "proposal-1", status: "PENDING" }], frozen_evidence_ids: ["evidence-1"], next_trigger: null } }))
  await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}`)
  await expect(page.getByTestId("ai-daily-report")).toContainText("每日报告")
  await expect(page.getByTestId("ai-daily-report")).toContainText("剧集 A")
  await expect(page.getByTestId("ai-daily-report")).toContainText("待确认 1 条")
})

test("切换优化师后每日报告请求保持租户边界", async ({ page }) => {
  const requested: string[] = []
  await page.route(`**/api/tenants/${tenant}/ai-optimizers`, (route) => route.fulfill({ json: { items: [{ id: optimizer, name: "手动优化师", active: true, version: 1, bc_id: "bc-a", advertiser_ids: ["acc-1"], strategy_version_id: "strategy-1", optimization_mode: "MANUAL", rule_ids: [] }] } }))
  await page.route("**/api/tenants/*/ai-optimizers/*/daily-report**", async (route) => { requested.push(route.request().url()); await route.fulfill({ json: { target_progress: {}, schedule_rows: [], material: {}, rule_actions: [], pending_proposals: [], frozen_evidence_ids: [] } }) })
  await page.goto(`/tenants/${tenant}/ai-workbench?optimizer_id=${optimizer}`)
  await expect.poll(() => requested.length).toBeGreaterThan(0)
  expect(requested.every((url) => url.includes(`/api/tenants/${tenant}/`))).toBeTruthy()
})

test("双租户双优化师切换保留 AUTO/MANUAL、下单路径和回读边界", async ({ page }) => {
  const tenantA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  const tenantB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
  const optimizerA = "aaaaaaaa-0000-4000-8000-aaaaaaaaaaaa"
  const optimizerAManual = "aaaaaaaa-0001-4000-8000-aaaaaaaaaaaa"
  const optimizerB = "bbbbbbbb-0000-4000-8000-bbbbbbbbbbbb"
  const reportRequests: string[] = []
  const proposalRequests: string[] = []
  const versionRequests: Array<Record<string, unknown>> = []
  const optimizerPayload = (id: string, mode: "AUTO" | "MANUAL", name: string) => ({
    id,
    name,
    active: true,
    version: mode === "AUTO" ? 2 : 3,
    bc_id: id.startsWith("bbbb") ? "bc-b" : "bc-a",
    advertiser_ids: [id.startsWith("bbbb") ? "acc-b" : "acc-a"],
    strategy_version_id: `strategy-${id}`,
    optimization_mode: mode,
    rule_ids: [id.startsWith("bbbb") ? "rule-b-account" : "rule-a-account"],
  })
  await page.route("**/api/tenants/*/ai-optimizers", async (route) => {
    const url = route.request().url()
    if (url.includes(`/tenants/${tenantA}/`)) {
      await route.fulfill({ json: { items: [optimizerPayload(optimizerA, "AUTO", "自动优化师 A"), optimizerPayload(optimizerAManual, "MANUAL", "手动优化师 A")] } })
      return
    }
    if (url.includes(`/tenants/${tenantB}/`)) {
      await route.fulfill({ json: { items: [optimizerPayload(optimizerB, "MANUAL", "手动优化师 B")] } })
      return
    }
    await route.fulfill({ status: 404, json: { detail: "cross-tenant optimizer lookup" } })
  })
  await page.route("**/api/tenants/*/ai-optimizers/*/daily-report**", async (route) => {
    const url = route.request().url()
    reportRequests.push(url)
    const tenantMatch = url.match(/\/api\/tenants\/([^/]+)\/ai-optimizers\/([^/]+)\//)
    const reportTenant = tenantMatch?.[1]
    const reportOptimizer = tenantMatch?.[2]
    const valid = (reportTenant === tenantA && [optimizerA, optimizerAManual].includes(reportOptimizer ?? "")) || (reportTenant === tenantB && reportOptimizer === optimizerB)
    if (!valid) {
      await route.fulfill({ status: 404, json: { detail: "cross-tenant report lookup" } })
      return
    }
    await route.fulfill({ json: {
      business_date: "2026-11-12",
      optimization_mode: reportOptimizer === optimizerA ? "AUTO" : "MANUAL",
      target_progress: { planned: 2, completed: 1, partial: 1, pending: 0, failed: 0, unknown: 0 },
      schedule_rows: [
        { id: `${reportOptimizer}-ordered`, title: "需下单剧", status: "PARTIAL", order_materials: true },
        { id: `${reportOptimizer}-existing`, title: "已有素材剧", status: "COMPLETE", order_materials: false },
      ],
      build_outcomes: [{ id: `${reportOptimizer}-build`, status: "PARTIAL", submission_id: `${reportOptimizer}-submission`, readback: { matched: 1, source: "tkada" } }],
      material: { requested: 1, not_requested: 1, pending: 0, matched: 1, unknown: 0 },
      rule_actions: [{ rule_id: reportOptimizer === optimizerB ? "rule-b-account" : "rule-a-account" }],
      pending_proposals: [{ id: `${reportOptimizer}-proposal`, status: "PENDING" }],
      frozen_evidence_ids: [`${reportOptimizer}-evidence`],
      next_trigger: null,
    } })
  })
  await page.route(`**/api/tenants/${tenantA}/ai-optimizers/${optimizerAManual}`, async (route) => {
    if (route.request().method() !== "PATCH") return route.continue()
    const body = route.request().postDataJSON() as Record<string, unknown>
    versionRequests.push(body)
    await route.fulfill({ json: { ...optimizerPayload(optimizerAManual, "MANUAL", "手动优化师 A"), version: 4, request_id: body.request_id } })
  })
  await page.route(`**/api/tenants/${tenantA}/ai-optimizers/${optimizerAManual}/proposals**`, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: [{ id: `${optimizerAManual}-proposal`, status: "PENDING", title: "复盘建议" }] })
      return
    }
    proposalRequests.push(route.request().url())
    await route.fulfill({ json: { id: `${optimizerAManual}-proposal`, status: "ACCEPTED", published_version_id: "strategy-next" } })
  })

  await page.goto(`/tenants/${tenantA}/ai-workbench?optimizer_id=${optimizerA}`)
  await expect(page.getByTestId("ai-daily-report")).toContainText("AUTO")
  await expect(page.getByTestId("ai-daily-report")).toContainText("部分完成")
  await expect(page.getByTestId("ai-daily-report")).toContainText("需下单剧")
  await page.goto(`/tenants/${tenantA}/ai-workbench?optimizer_id=${optimizerAManual}`)
  await expect(page.getByTestId("ai-daily-report")).toContainText("MANUAL")
  await expect(page.getByTestId("ai-daily-report")).toContainText("部分完成")
  const nextVersion = await page.evaluate(async ({ tenantId, optimizerId }) => {
    const response = await fetch(`/api/tenants/${tenantId}/ai-optimizers/${optimizerId}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ strategy_version_id: "strategy-next", request_id: "next-version-request" }),
    })
    return { ok: response.ok, payload: await response.json() }
  }, { tenantId: tenantA, optimizerId: optimizerAManual })
  expect(nextVersion.ok).toBeTruthy()
  expect(nextVersion.payload.version).toBe(4)
  await page.goto(`/tenants/${tenantA}/ai-workbench?optimizer_id=${optimizerAManual}&view=pending`)
  await expect(page.getByTestId("ai-pending-view")).toContainText("复盘建议")
  await page.getByRole("button", { name: "接受" }).click()
  await expect.poll(() => proposalRequests.length).toBe(1)
  expect(proposalRequests[0]).toContain(`/proposals/${optimizerAManual}-proposal/accept`)
  expect(versionRequests).toEqual([{ strategy_version_id: "strategy-next", request_id: "next-version-request" }])
  await page.goto(`/tenants/${tenantB}/ai-workbench?optimizer_id=${optimizerB}`)
  // 优化师名称显示在左侧列表；报告卡片只承载模式与冻结证据。
  await expect(page.getByTestId("ai-workbench-shell")).toContainText("手动优化师 B")
  await expect(page.getByTestId("ai-daily-report")).toContainText("MANUAL")
  await expect.poll(() => reportRequests.length).toBeGreaterThanOrEqual(3)
  expect(reportRequests.every((url) => url.includes("/api/tenants/") && !url.includes(`/tenants/${tenantA}/ai-optimizers/${optimizerB}/`))).toBeTruthy()
  expect(reportRequests.some((url) => url.includes(`/tenants/${tenantB}/ai-optimizers/${optimizerB}/`))).toBeTruthy()
})
