import type { ManualLinkInput } from "@/client"

export type NamedManualLink = Omit<ManualLinkInput, "line_no"> & {
  title: string
}

export function validMinisUrl(value: string) {
  try {
    const url = new URL(value)
    return (
      !/\s/.test(value) &&
      url.protocol === "https:" &&
      ["tiktok.com", "www.tiktok.com"].includes(url.hostname) &&
      !url.username &&
      !url.password &&
      (!url.port || url.port === "443") &&
      /^\/minis\/[^/]+\/?$/.test(url.pathname)
    )
  } catch {
    return false
  }
}

// Excel 的 Tab 分列、换行分行；不按空格拆分，避免破坏剧名和链接参数。
export function parseManualPaste(text: string): NamedManualLink[] {
  const rows = text
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .filter((row) => row.trim())
  if (rows.length > 1000) throw new Error("一次最多添加 1000 部剧")
  const seen = new Set<string>()
  return rows.map((row, i) => {
    const columns = row.split("\t").map((value) => value.trim())
    const [title, url = "", external_drama_id = "", protected_base = ""] =
      columns
    if (!title || columns.length < 2 || columns.length > 4)
      throw new Error(`第 ${i + 1} 行请按「剧名、推广链接」两列粘贴，不含表头`)
    if (seen.has(title))
      throw new Error(`第 ${i + 1} 行剧名重复，请核对后每部剧保留一行`)
    if (!validMinisUrl(url))
      throw new Error(`第 ${i + 1} 行请填写完整的 HTTPS TikTok Minis 推广链接`)
    if (
      title.length > 1000 ||
      url.length > 8192 ||
      external_drama_id.length > 255 ||
      protected_base.length > 1000
    )
      throw new Error(`第 ${i + 1} 行内容过长，请检查`)
    seen.add(title)
    return { title, url, external_drama_id, protected_base }
  })
}

export function linksForLines(
  text: string,
  links: NamedManualLink[],
): ManualLinkInput[] {
  const lines = text.split("\n").map((title) => title.trim())
  return links.map(({ title, ...link }) => {
    const first = lines.indexOf(title)
    if (first < 0 || lines.lastIndexOf(title) !== first)
      throw new Error(
        `「${title}」的剧名已修改或重复，请在批量添加中更新对应链接`,
      )
    return { ...link, line_no: first + 1 }
  })
}
