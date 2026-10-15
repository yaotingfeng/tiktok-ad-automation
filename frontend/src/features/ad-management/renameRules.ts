import type { RenameRule } from "@/client"

// 改名规则的纯函数：不依赖界面组件，供改名面板和测试共用。

// 与后端 MAX_RENAME_ROWS 一致：表格粘贴单次最多 500 行。
const MAX_RENAME_ROWS = 500
const DIGITS = /^\d+$/

export type RenameKind = RenameRule["kind"]

/** 改名面板的输入草稿；点击“生成预览”时才转成改名规则提交给服务器。 */
export type RenameDraft = {
  kind: RenameKind
  find: string
  replaceWith: string
  prefix: string
  suffix: string
  text: string
}

export type PastedRow = { campaignId: string; newName: string }

export function emptyRenameDraft(kind: RenameKind): RenameDraft {
  return { kind, find: "", replaceWith: "", prefix: "", suffix: "", text: "" }
}

/**
 * 解析从飞书表格或导出报表复制来的两列：推广系列 ID、新名称。
 * 一行一条；有 Tab 时按 Tab 分列，否则按第一个逗号分列（新名称里可以含逗号）。
 * 首行第一列不是纯数字时视为表头忽略；ID 格式本身由服务器校验并逐行标出。
 */
export function parsePastedRows(text: string): {
  rows: PastedRow[]
  errors: string[]
} {
  const rows: PastedRow[] = []
  const errors: string[] = []
  const lines = text.split(/\r?\n/)
  let first = true
  lines.forEach((line, index) => {
    if (!line.trim()) return
    const separator = line.includes("\t") ? "\t" : ","
    const at = line.indexOf(separator)
    const campaignId = (at < 0 ? line : line.slice(0, at)).trim()
    const rest = at < 0 ? "" : line.slice(at + 1)
    // Tab 分列时只取第二列，忽略表格里多带的其它列。
    const newName = (separator === "\t" ? rest.split("\t")[0] : rest).trim()
    const header = first && !DIGITS.test(campaignId)
    first = false
    if (header) return
    if (!campaignId || !newName) {
      errors.push(`第 ${index + 1} 行缺少推广系列 ID 或新名称`)
      return
    }
    rows.push({ campaignId, newName })
  })
  if (rows.length > MAX_RENAME_ROWS) errors.push("单次最多 500 行，请分批粘贴")
  return { rows, errors }
}

/** 草稿对应的改名规则；必填项缺失或表格有解析错误时返回 null，不生成预览。 */
export function renameRuleFrom(draft: RenameDraft): RenameRule | null {
  if (draft.kind === "replace")
    return draft.find
      ? { kind: "replace", find: draft.find, replace_with: draft.replaceWith }
      : null
  if (draft.kind === "affix")
    return draft.prefix || draft.suffix
      ? { kind: "affix", prefix: draft.prefix, suffix: draft.suffix }
      : null
  const { rows, errors } = parsePastedRows(draft.text)
  if (errors.length > 0 || rows.length === 0) return null
  return {
    kind: "explicit",
    names: Object.fromEntries(rows.map((row) => [row.campaignId, row.newName])),
  }
}
