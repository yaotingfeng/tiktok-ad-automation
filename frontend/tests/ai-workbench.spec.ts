import { expect, test } from "@playwright/test"
import { canWriteAutomationContext, mergeAutomationSearch } from "../src/features/automation-rules/AutomationRulesPage"
import { filterBoundRules } from "../src/features/automation-rules/RuleList"
import { strategyContextSearch } from "../src/features/strategies/StrategyForm"
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
// 必须排在其他组件模块之后：先加载 navigation 会让 Playwright 的 Node 端转换
// 先解析 lucide-react 源码入口，导致 react.createContext 未定义而整份用例无法加载。
import { workspaceNavigation } from "../src/components/Sidebar/navigation"

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
  const groups = workspaceNavigation({ prefix: "/tenants/tenant-a", manage: true, platformAdmin: true })
  const aiIndex = groups.findIndex((group) => group.label === "AI 投手")
  const settingsIndex = groups.findIndex((group) => group.label === "设置")
  expect(aiIndex).toBeGreaterThan(settingsIndex)
  expect(groups[aiIndex]?.items[0]?.path).toBe("/tenants/tenant-a/ai-workbench")
  expect(groups.find((group) => group.label === "广告投放")?.items.some((item) => item.title === "AI 投手")).toBe(false)
})

test("AI 二级菜单链接保留租户、投手和返回上下文", () => {
  expect(aiWorkbenchMenu.map((item) => item.id)).toEqual([
    "overview",
    "schedule",
    "execution",
    "review",
    "knowledge",
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


test("切换投手会清掉旧的记录、版本和返回上下文", () => {
  expect(buildAIWorkbenchSwitchSearch({ optimizer_id: "old", record_id: "r", version_id: "v", return_to: "/old" }, "new")).toEqual({ optimizer_id: "new", record_id: undefined, version_id: undefined, return_to: undefined })
})

test("执行视图使用租户和投手作用域的 occurrence API", () => {
  expect(aiOccurrenceEndpoint("tenant-a", "optimizer-b")).toBe("/api/tenants/tenant-a/ai-optimizers/optimizer-b/occurrences")
})


test("投手没有绑定规则时不会泄露租户规则", () => {
  expect(filterBoundRules([{ id: "rule-a" }, { id: "rule-b" }], true, [])).toEqual([])
  expect(filterBoundRules([{ id: "rule-a" }, { id: "rule-b" }], true, ["rule-b"])).toEqual([{ id: "rule-b" }])
})


test("AI 绑定规则上下文禁止待确认写操作", () => {
  expect(canWriteAutomationContext(true, true)).toBe(false)
  expect(canWriteAutomationContext(true, false)).toBe(true)
})


test("策略编辑上下文同时保留投手、记录、版本和返回地址", () => {
  expect(strategyContextSearch({ aiOptimizerId: "optimizer-b", recordId: "record-c", versionId: "version-d", returnContext: "/tenants/tenant-a/ai-workbench", readonly: true, scope: "bound" })).toEqual({ ai_optimizer_id: "optimizer-b", record_id: "record-c", version_id: "version-d", return_to: "/tenants/tenant-a/ai-workbench", readonly: true, scope: "bound" })
})

test("自动规则切换标签合并而不是覆盖 AI 搜索上下文", () => {
  expect(mergeAutomationSearch({ ai_optimizer_id: "optimizer-b", record_id: "record-c", return_to: "/ai" }, { tab: "runs" })).toEqual({ ai_optimizer_id: "optimizer-b", record_id: "record-c", return_to: "/ai", tab: "runs" })
})

test("冻结排期行不能通过 TSV 覆盖", () => {
  const rows = [
    {
      ...emptyPlanRow("2026-11-12", "optimizer-a"),
      id: "stable-row",
      read_only: true,
      title: "已提交剧目",
    },
  ]
  expect(() =>
    applyPlanPaste(rows, 0, 0, "09:00\t覆盖", "2026-11-12", "optimizer-a"),
  ).toThrow("只读行")
  expect(rows[0].title).toBe("已提交剧目")
})


test("租户管理员权限不能替代平台管理员的 AI 灰度权限", () => {
  expect(workspaceNavigation({ prefix: "/tenants/tenant-a", manage: true }).some(group => group.items.some(item => item.path?.endsWith("/ai-workbench")))).toBe(false)
})
