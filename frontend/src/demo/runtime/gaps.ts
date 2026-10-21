// 体验版遇到没有覆盖的请求或跳转时，集中记录并通知外壳醒目提示，绝不放行到网络。
export const DEMO_GAP_EVENT = "tk-ada-demo:gap"

const seen: string[] = []
// 端到端测试从页面读取未覆盖记录。
;(window as unknown as { __tkAdaDemoGaps: string[] }).__tkAdaDemoGaps = seen

export function reportDemoGap(description: string) {
  console.error(`[demo] 未覆盖：${description}`)
  seen.push(description)
  window.dispatchEvent(new CustomEvent(DEMO_GAP_EVENT, { detail: description }))
}

/** 端到端测试读取这个列表，断言整条流程没有未覆盖的操作。 */
export function demoGaps() {
  return [...seen]
}
