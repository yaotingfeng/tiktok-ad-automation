// 引导卡片上的“一键操作”：替体验者完成选择、填写等步骤，让演示只需要点按钮。
// 只通过页面公开的交互（输入事件、点击）驱动生产页面，不改生产组件。

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

async function waitFor<T>(find: () => T | null | undefined, timeout = 4000) {
  const started = Date.now()
  while (Date.now() - started < timeout) {
    const found = find()
    if (found) return found
    await wait(100)
  }
  return null
}

const visible = (el: Element) => {
  const rect = el.getBoundingClientRect()
  return rect.width > 0 && rect.height > 0
}

/** 给 React 受控输入框赋值：走原生 setter 再派发 input 事件，React 才会更新状态。 */
export function fillField(
  selector: string | Element | null | undefined,
  value: string,
) {
  const el = (
    typeof selector === "string" ? document.querySelector(selector) : selector
  ) as HTMLInputElement | HTMLTextAreaElement | null
  if (!el) return false
  const proto =
    el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype
  Object.getOwnPropertyDescriptor(proto, "value")?.set?.call(el, value)
  el.dispatchEvent(new Event("input", { bubbles: true }))
  return true
}

/** 模拟一次完整的鼠标点击（Radix 组件在 pointerdown 时响应）。 */
function press(el: Element) {
  const init = {
    bubbles: true,
    cancelable: true,
    button: 0,
    pointerType: "mouse",
  }
  el.dispatchEvent(new PointerEvent("pointerdown", init))
  el.dispatchEvent(new MouseEvent("mousedown", init))
  el.dispatchEvent(new PointerEvent("pointerup", init))
  el.dispatchEvent(new MouseEvent("mouseup", init))
  el.dispatchEvent(new MouseEvent("click", init))
}

/** 打开标签为 label 的下拉框，并选中第一个文字包含 option 的选项（省略则选第一项）。 */
export async function chooseOption(label: string, option?: string) {
  const trigger = Array.from(
    document.querySelectorAll<HTMLElement>('[role="combobox"]'),
  ).find(
    (el) =>
      visible(el) &&
      (el.getAttribute("aria-label") === label ||
        el
          .closest('[data-slot="field"], [role="group"], div')
          ?.querySelector("label")
          ?.textContent?.trim() === label),
  )
  if (!trigger) return false
  if (trigger.getAttribute("aria-expanded") !== "true") press(trigger)
  const target = await waitFor(() =>
    Array.from(document.querySelectorAll('[role="option"]')).find(
      (el) =>
        visible(el) &&
        el.getAttribute("aria-disabled") !== "true" &&
        (!option || el.textContent?.includes(option)),
    ),
  )
  if (!target) return false
  press(target)
  await wait(400)
  return true
}

/**
 * 依次完成多个选择；前一个选择会解锁后一个下拉框，候选项可能还在加载，失败时稍等重试。
 * quiet：自动填写示例数据时隐藏中途弹出的选择弹窗，体验者只看到填好的结果。
 */
export async function chooseOptions(
  pairs: [string, string?][],
  options: { quiet?: boolean } = {},
) {
  if (options.quiet) document.documentElement.dataset.demoAutofill = "true"
  try {
    await choosePairs(pairs)
  } finally {
    delete document.documentElement.dataset.demoAutofill
  }
}

async function choosePairs(pairs: [string, string?][]) {
  for (const [label, option] of pairs) {
    for (let attempt = 0; attempt < 4; attempt++) {
      if (await chooseOption(label, option)) break
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))
      await wait(700)
    }
    await wait(500)
  }
}
