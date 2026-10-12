import { expect, type Locator } from "@playwright/test"

/** 断言趋势图真的画出了折线：路径非空且描边颜色有效（不是 none/透明）。 */
export async function expectLineDrawn(chart: Locator) {
  const curve = chart.locator(".recharts-line-curve").first()
  await expect(curve).toHaveAttribute("d", /\S+/)
  const stroke = await curve.evaluate((el) => getComputedStyle(el).stroke)
  expect(stroke).not.toBe("none")
  expect(stroke).not.toBe("rgba(0, 0, 0, 0)")
}
