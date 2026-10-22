import { chooseOptions, fillField } from "../tour/actions"
import { fieldInput, topDialog } from "../tour/find"

// 体验版示例数据：需要输入的地方都能一键填好，自由体验时也不用自己找剧名、账户 ID。
// 剧名和账户来自演示数据（fixtures/world.ts），能被搭建、素材匹配等接口识别。
export const SAMPLE_DRAMAS = [
  "The Billionaire's Secret Bride",
  "Reborn as the Alpha's Mate",
]
export const SAMPLE_ACCOUNTS = ["7431000000000001001", "7431000000000001002"]
export const SAMPLE_PROVIDER = "网眼 · 主账号"
export const SAMPLE_STRATEGY = "美区 · 目标 ROAS 1.1"

// 弹窗是 position: fixed，offsetParent 恒为 null，按实际尺寸判断是否可见。
const visible = (el: Element | null | undefined): el is HTMLElement => {
  if (!el) return false
  const rect = el.getBoundingClientRect()
  return rect.width > 0 && rect.height > 0
}

const isEmpty = (el: Element | null | undefined) =>
  !(el as HTMLInputElement | null)?.value?.trim()

/** 批量搭建输入页：版权方连接、推广应用、投放策略、剧目、账户全部填好。 */
export async function fillBuildInput(options: { quiet?: boolean } = {}) {
  await chooseOptions(
    [
      ["版权方连接", SAMPLE_PROVIDER],
      ["推广应用"],
      ["投放策略", SAMPLE_STRATEGY],
    ],
    options,
  )
  fillField("#build-drama", SAMPLE_DRAMAS.join("\n"))
  fillField("#build-account", SAMPLE_ACCOUNTS.join("\n"))
}

interface SampleFiller {
  /** 按钮上显示的说明。 */
  label: string
  applies: () => boolean
  run: () => void | Promise<void>
}

const dialogTitle = () =>
  topDialog()?.querySelector("h2")?.textContent?.trim() ?? ""

// 按当前页面或弹窗判断能填什么；越具体的放越前面（弹窗优先于页面）。
const FILLERS: SampleFiller[] = [
  {
    label: "填入示例新值",
    applies: () => visible(document.getElementById("management-value")),
    run: () => {
      const title = dialogTitle()
      fillField(
        "#management-value",
        title.includes("ROAS")
          ? "1.25"
          : title.includes("预算")
            ? "250"
            : "演示-",
      )
    },
  },
  {
    label: "填入示例规则名称",
    applies: () => visible(fieldInput("规则名称")()) && !!topDialog(),
    run: () => {
      const input = fieldInput("规则名称")()
      if (isEmpty(input)) fillField(input, "演示 · 剧级 ROAS 止损")
    },
  },
  {
    label: "填入示例策略",
    applies: () => visible(fieldInput("策略名称")()),
    run: () => {
      const name = fieldInput("策略名称")()
      if (isEmpty(name)) fillField(name, "体验策略 · 美区加量测试")
      const budget = fieldInput("日预算")()
      if (isEmpty(budget)) fillField(budget, "120")
    },
  },
  {
    label: "填入示例剧目和账户",
    applies: () => visible(document.getElementById("build-drama")),
    run: () => fillBuildInput({ quiet: true }),
  },
]

export function currentFiller() {
  return FILLERS.find((filler) => {
    try {
      return filler.applies()
    } catch {
      return false
    }
  })
}
