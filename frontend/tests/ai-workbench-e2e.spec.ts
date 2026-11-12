import { expect, test } from "@playwright/test"

const tenant = "11111111-1111-4111-8111-111111111111"
const optimizer = "22222222-2222-4222-8222-222222222222"

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
