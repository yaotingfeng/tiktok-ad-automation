// 引导步骤定义。每一步都指向页面上一个具体元素：要么请体验者点击它（click），
// 要么解释它（next）。找不到元素时卡片给出“回到上一步 / 跳过”的恢复入口。
export interface TourContext {
  tenantId: string
  bcId: string
  /** 当前路由路径（不含 hash 前缀与查询参数）。 */
  pathname: string
}

export type TourTarget =
  | string
  | ((context: TourContext) => Element | null | undefined)

export interface TourStep {
  title: string
  body: string
  /**
   * 这一步所在的页面。进入步骤时若当前不在该页面会先跳过去；
   * 省略则停留在当前页（例如紧接着上一步点击打开的弹窗）。
   */
  path?: string | ((context: TourContext) => string)
  target?: TourTarget
  /**
   * next：看完说明点“下一步”；
   * click：请体验者点击高亮元素，点击后自动进入下一步（也可以“跳过”）。
   */
  advance?: "next" | "click"
  /** click 步骤要点的按钮名称，卡片上醒目提示，例如「创建并立即启用」。 */
  cta?: string
  /** 点击后等待多少毫秒再进入下一步（等弹窗打开、页面跳转）。 */
  settleMs?: number
  /** 页面就绪前不允许“下一步”（例如上传完成、预览生成），最多等 25 秒。 */
  readyWhen?: () => boolean
  /** 等待就绪时卡片上的说明。 */
  waitingText?: string
  /** 卡片上的辅助按钮，例如“使用示例视频”“帮我填写”。 */
  action?: {
    label: string
    run: (context: TourContext) => void | Promise<void>
  }
  /** 卡片相对高亮元素的位置偏好。 */
  placement?: "bottom" | "top" | "left" | "right"
}

export interface TourChapter {
  id: string
  title: string
  /** 章节简介，显示在引导目录里。 */
  summary: string
  steps: TourStep[]
}

export interface Tour {
  id: string
  title: string
  steps: (TourStep & { chapter?: string })[]
}
