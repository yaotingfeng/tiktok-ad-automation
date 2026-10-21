import type { TourContext, TourTarget } from "./types"

// 引导定位优先用页面上可见的文字与角色，避免为体验版改动生产页面。
const visible = (el: Element) => {
  const rect = el.getBoundingClientRect()
  if (rect.width === 0 && rect.height === 0) return false
  const style = window.getComputedStyle(el)
  return style.visibility !== "hidden" && style.display !== "none"
}

const normalize = (text: string | null | undefined) =>
  (text ?? "").replace(/\s+/g, "").trim()

/** 按可见文字查找元素：selector 限定元素类型，text 为字符串（包含匹配）或正则。 */
export function byText(
  selector: string,
  text: string | RegExp,
  options: { exact?: boolean; within?: string; last?: boolean } = {},
) {
  return () => {
    const scope = options.within
      ? Array.from(document.querySelectorAll(options.within)).filter(visible)
      : [document]
    const matches: Element[] = []
    for (const root of scope)
      for (const el of Array.from(root.querySelectorAll(selector))) {
        if (!visible(el) || el.closest("[data-demo-shell]")) continue
        const content = normalize(
          (el as HTMLElement).innerText ?? el.textContent,
        )
        const label = normalize(el.getAttribute("aria-label"))
        const hit =
          typeof text === "string"
            ? options.exact
              ? content === normalize(text) || label === normalize(text)
              : content.includes(normalize(text)) ||
                label.includes(normalize(text))
            : text.test(content) || text.test(label)
        if (hit) matches.push(el)
      }
    return options.last ? matches[matches.length - 1] : matches[0]
  }
}

/** 侧栏导航项。 */
export const navItem = (label: string) =>
  byText("[data-sidebar] a, nav a", label, { exact: true })

/** 按钮（含链接按钮）。 */
export const button = (label: string | RegExp, exact = false) =>
  byText("button, a[role=button], a[data-slot=button]", label, { exact })

export function resolveTarget(
  target: TourTarget | undefined,
  context: TourContext,
) {
  if (!target) return null
  if (typeof target === "string") {
    // 引导卡片本身也是 role=dialog，定位页面弹窗时要排除体验版外壳。
    const el = Array.from(document.querySelectorAll(target)).find(
      (item) => visible(item) && !item.closest("[data-demo-shell]"),
    )
    return el ?? null
  }
  try {
    return target(context) ?? null
  } catch {
    return null
  }
}

/** 当前最上层的页面弹窗（不含体验版自己的引导卡片）。 */
export function topDialog() {
  const dialogs = Array.from(
    document.querySelectorAll('[role="dialog"], [role="alertdialog"]'),
  ).filter((el) => visible(el) && !el.closest("[data-demo-shell]"))
  return dialogs[dialogs.length - 1] ?? null
}

/** 最上层弹窗里的元素。 */
export const inDialog = (selector: string, text?: string | RegExp) => () => {
  const dialog = topDialog()
  if (!dialog) return null
  const items = Array.from(dialog.querySelectorAll(selector)).filter(visible)
  if (text === undefined) return items[0] ?? null
  return (
    items.find((el) => {
      const content = normalize((el as HTMLElement).innerText ?? el.textContent)
      return typeof text === "string"
        ? content.includes(normalize(text))
        : text.test(content)
    }) ?? null
  )
}

/** 最上层弹窗本身。 */
export const dialogTarget = () => topDialog()

/** 弹窗右上角的关闭按钮。 */
export const dialogClose = () => {
  const dialog = topDialog()
  if (!dialog) return null
  return (
    Array.from(dialog.querySelectorAll("button")).find(
      (el) => el.textContent?.trim() === "Close",
    ) ?? null
  )
}

/** 按字段标签找输入框（label for → 输入框，或同一字段里的第一个输入框）。 */
export const fieldInput = (label: string) => () => {
  const labels = Array.from(document.querySelectorAll("label")).filter(
    (el) => visible(el) && normalize(el.textContent) === normalize(label),
  )
  for (const el of labels) {
    const id = el.getAttribute("for")
    const byId = id ? document.getElementById(id) : null
    if (byId) return byId
    const field = el.closest('[data-slot="field"]') ?? el.parentElement
    const input = field?.querySelector("input, textarea")
    if (input) return input
  }
  return null
}

/** 主内容区表格的第 n 行（从 0 开始），可按行内文字筛选。 */
export const tableRow =
  (n = 0, text?: string) =>
  () => {
    // 跳过空行（加载占位、折叠区域里的表格）。
    const rows = Array.from(document.querySelectorAll("main tbody tr")).filter(
      (row) => {
        const content = normalize((row as HTMLElement).innerText)
        return (
          visible(row) &&
          content.length > 0 &&
          (!text || content.includes(normalize(text)))
        )
      },
    )
    return rows[n] ?? null
  }

/** 主内容区表格某一行里的按钮或复选框。 */
export const inRow =
  (selector: string, n = 0, text?: string) =>
  () => {
    const row = tableRow(n)()
    if (!row) return null
    const items = Array.from(row.querySelectorAll(selector)).filter(visible)
    if (!text) return items[0] ?? null
    return (
      items.find((el) =>
        normalize((el as HTMLElement).innerText).includes(normalize(text)),
      ) ?? null
    )
  }

/** 包含某段文字的卡片（shadcn Card）。 */
export const card = (text: string) => () => {
  const cards = Array.from(
    document.querySelectorAll('main [data-slot="card"]'),
  ).filter(
    (el) => visible(el) && normalize(el.textContent).includes(normalize(text)),
  )
  // 取最内层的那张，避免高亮整页外壳。
  return cards[cards.length - 1] ?? null
}

/** 页面主内容里的某段文字是否出现（用于 readyWhen）。 */
export const mainHas = (text: string | RegExp) => () => {
  const content = document.querySelector("main")?.textContent ?? ""
  return typeof text === "string" ? content.includes(text) : text.test(content)
}
