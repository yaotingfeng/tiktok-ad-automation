import type { RenameRule } from "@/client"

// 改名规则的纯函数：不依赖界面组件，供改名面板和测试共用。

// 与后端 MAX_RENAME_ROWS 一致：表格粘贴单次最多 500 行。
const MAX_RENAME_ROWS = 500
// 含任意半角或全角数字的首列不当表头，交给服务器判定 ID 格式（如 1.7E+18 会被标为 id_invalid）。
const ANY_DIGIT = /[\d０-９]/

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
 * 首行第一列不含任何数字时视为表头忽略；ID 格式本身由服务器校验并逐行标出。
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
    const header = first && !ANY_DIGIT.test(campaignId)
    first = false
    if (header) return
    if (!campaignId || !newName) {
      // 首列里出现空格或全角逗号，通常是用错了分隔符，提示改用 Tab 或半角逗号。
      errors.push(
        /[\s，]/.test(campaignId)
          ? `第 ${index + 1} 行未分出两列，请用 Tab 或逗号分隔推广系列 ID 和新名称`
          : `第 ${index + 1} 行缺少推广系列 ID 或新名称`,
      )
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
  // 同一 ID 出现多次时保留第一行，与服务器一致（后面的行被标为 duplicate_campaign_id）。
  const names: Record<string, string> = {}
  for (const row of rows)
    if (!(row.campaignId in names)) names[row.campaignId] = row.newName
  return { kind: "explicit", names }
}
