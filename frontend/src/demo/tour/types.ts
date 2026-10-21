// 引导步骤定义。target 定位要高亮的元素；找不到时卡片居中显示，不阻塞引导。
export interface TourContext {
  tenantId: string
  bcId: string
  /** 当前路由路径（不含 hash 前缀）。 */
  pathname: string
}

export type TourTarget =
  | string
  | ((context: TourContext) => Element | null | undefined)

export interface TourStep {
  title: string
  body: string
  /** 进入这一步前跳转到的站内路径；省略则停留在当前页。 */
  path?: string | ((context: TourContext) => string)
  target?: TourTarget
  /**
   * next：点“下一步”继续；
   * click：等待体验者点击高亮元素后自动进入下一步（仍可点“下一步”跳过）。
   */
  advance?: "next" | "click"
  /** 进入下一步前额外等待的毫秒数（例如点击后等弹窗打开）。 */
  settleMs?: number
  /**
   * 等页面进入某个状态后才允许“下一步”（例如上传完成、任务成功），避免演示时中途离开页面。
   * 最多等 20 秒，之后无论如何放行。
   */
  readyWhen?: () => boolean
  /** 卡片上的辅助按钮，例如“使用示例视频”替体验者选好文件。 */
  action?: {
    label: string
    run: (context: TourContext) => void | Promise<void>
  }
  /** 卡片相对高亮元素的位置偏好。 */
  placement?: "bottom" | "top" | "left" | "right"
}

export interface Tour {
  id: string
  title: string
  steps: TourStep[]
}
