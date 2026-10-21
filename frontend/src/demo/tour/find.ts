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
