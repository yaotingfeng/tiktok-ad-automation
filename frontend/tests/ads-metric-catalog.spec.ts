import { expect, test } from "@playwright/test"
import {
  formatMetric,
  isRecoveryOpen,
  recoveryDayOf,
} from "../src/features/ads/metricCatalog"
import { statusLabel } from "../src/features/ads/statusLabel"

test("官方天数映射", () => {
  expect(recoveryDayOf("roas_d3")).toBe(2)
  expect(recoveryDayOf("revenue_d30")).toBe(29)
  expect(recoveryDayOf("spend")).toBeNull()
})

test("回收窗口按最晚的账户本地日判断", () => {
  const now = new Date("2026-10-12T01:00:00Z")
  // UTC 已是 10-12，洛杉矶仍是 10-11：结束日 10-10 时第 2 天窗口对洛杉矶账户未结束。
  expect(isRecoveryOpen(2, "2026-10-10", ["UTC"], now)).toBe(false)
  expect(
    isRecoveryOpen(2, "2026-10-10", ["UTC", "America/Los_Angeles"], now),
  ).toBe(true)
})

test("指标格式化", () => {
  expect(formatMetric("0.1234", "ratio")).toBe("12.34%")
  expect(formatMetric("1234", "count")).toBe("1,234")
  expect(formatMetric(null, "money")).toBe("—")
})

test("状态中文映射", () => {
  expect(statusLabel({ operation_status: "DISABLE" }).label).toBe("已暂停")
  expect(
    statusLabel({
      operation_status: "ENABLE",
      secondary_status: "CAMPAIGN_STATUS_ENABLE",
    }).label,
  ).toBe("投放中")
  expect(statusLabel({ operation_status: "SOMETHING_NEW" }).label).toBe(
    "SOMETHING_NEW",
  )
})
