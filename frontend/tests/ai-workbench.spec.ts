import { expect, test } from "@playwright/test"
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
