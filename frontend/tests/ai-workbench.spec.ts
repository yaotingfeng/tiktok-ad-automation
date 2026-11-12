import { expect, test } from "@playwright/test"
import { workspaceNavigation } from "../src/components/Sidebar/navigation"
import { canWriteAutomationContext } from "../src/features/automation-rules/AutomationRulesPage"
import { filterBoundRules } from "../src/features/automation-rules/RuleList"
import {
  aiWorkbenchMenu,
  aiOccurrenceEndpoint,
  buildAIWorkbenchHref,
  buildAIWorkbenchSwitchSearch,
} from "../src/features/ai-workbench/AIWorkbenchShell"

import {
  applyPlanPaste,
  emptyPlanRow,
} from "../src/features/ai-workbench/DramaPlanGrid"

test("AI 剧单工作表默认十行并保留 TSV 文本值", () => {
  const rows = Array.from({ length: 10 }, () =>
    emptyPlanRow("2026-11-12", "optimizer-a"),
  )
  const next = applyPlanPaste(
    rows,
    0,
    0,
    '08:30\t剧集 A\tconnection-a\tapp-a\t\t"0012"\t是',
    "2026-11-12",
    "optimizer-a",
  )
  expect(next).toHaveLength(10)
  expect(next[0].external_drama_id).toBe("0012")
  expect(next[0].order_materials).toBe(true)
})

test("AI 剧单粘贴可以扩展到十行之外并保持租户上下文", () => {
  const next = applyPlanPaste(
    [emptyPlanRow("2026-11-12", "optimizer-a")],
    9,
    1,
    "剧集 B",
    "2026-11-12",
    "optimizer-a",
  )
  expect(next).toHaveLength(10)
  expect(next[9].optimizer_id).toBe("optimizer-a")
  expect(next[9].title).toBe("剧集 B")
})

test("AI 入口是体验版之前的独立二级菜单，不混入广告投放", () => {
  const groups = workspaceNavigation({ prefix: "/tenants/tenant-a", manage: true })
  const aiIndex = groups.findIndex((group) => group.label === "AI 优化师")
  const settingsIndex = groups.findIndex((group) => group.label === "设置")
  expect(aiIndex).toBeGreaterThan(settingsIndex)
  expect(groups[aiIndex]?.items[0]?.path).toBe("/tenants/tenant-a/ai-workbench")
  expect(groups.find((group) => group.label === "广告投放")?.items.some((item) => item.title === "AI 优化师")).toBe(false)
})

test("AI 二级菜单链接保留租户、优化师和返回上下文", () => {
  expect(aiWorkbenchMenu.map((item) => item.id)).toEqual([
    "overview",
    "schedule",
    "execution",
    "review",
    "pending",
  ])
  const href = buildAIWorkbenchHref(
    "tenant-a",
    "optimizer-b",
    "review",
    "/tenants/tenant-a/ai-workbench?optimizer_id=optimizer-b",
  )
  expect(href).toContain("/tenants/tenant-a/ai-workbench?")
  expect(href).toContain("optimizer_id=optimizer-b")
  expect(href).toContain("view=review")
  expect(href).toContain("return_to=")
})


test("切换优化师会清掉旧的记录、版本和返回上下文", () => {
  expect(buildAIWorkbenchSwitchSearch({ optimizer_id: "old", record_id: "r", version_id: "v", return_to: "/old" }, "new")).toEqual({ optimizer_id: "new", record_id: undefined, version_id: undefined, return_to: undefined })
})

test("执行视图使用租户和优化师作用域的 occurrence API", () => {
  expect(aiOccurrenceEndpoint("tenant-a", "optimizer-b")).toBe("/api/tenants/tenant-a/ai-optimizers/optimizer-b/occurrences")
})


test("优化师没有绑定规则时不会泄露租户规则", () => {
  expect(filterBoundRules([{ id: "rule-a" }, { id: "rule-b" }], true, [])).toEqual([])
  expect(filterBoundRules([{ id: "rule-a" }, { id: "rule-b" }], true, ["rule-b"])).toEqual([{ id: "rule-b" }])
})


test("AI 绑定规则上下文禁止待确认写操作", () => {
  expect(canWriteAutomationContext(true, true)).toBe(false)
  expect(canWriteAutomationContext(true, false)).toBe(true)
})
