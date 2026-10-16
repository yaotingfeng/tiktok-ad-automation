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
  // UTC 已是 10-12，从 10-09 算起 3 天已过，第 2 天窗口已结束；洛杉矶仍是 10-11（2 天）窗口未结束。
  expect(isRecoveryOpen(2, "2026-10-09", ["UTC"], now)).toBe(false)
  expect(
    isRecoveryOpen(2, "2026-10-09", ["UTC", "America/Los_Angeles"], now),
  ).toBe(true)
  // 第 0 天：当日开启，次日关闭
  expect(isRecoveryOpen(0, "2026-10-12", ["UTC"], now)).toBe(true)
  expect(isRecoveryOpen(0, "2026-10-11", ["UTC"], now)).toBe(false)
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
  ).toBe("已开启")
  // 只有平台明确的投放状态才显示"投放中"。
  expect(
    statusLabel({
      operation_status: "ENABLE",
      secondary_status: "ADGROUP_STATUS_DELIVERY_OK",
    }).label,
  ).toBe("投放中")
  // 剧行按成员汇总，附开启系列数。
  expect(
    statusLabel({
      operation_status: "ENABLE",
      campaign_count: "18",
      enabled_campaign_count: "3",
    }).label,
  ).toBe("已开启 3/18")
  expect(statusLabel({ operation_status: "SOMETHING_NEW" }).label).toBe(
    "SOMETHING_NEW",
  )
  // 边界用例：secondary 覆盖 operation
  expect(
    statusLabel({ secondary_status: "CAMPAIGN_STATUS_DISABLE" }).label,
  ).toBe("已暂停")
  expect(
    statusLabel({ secondary_status: "ADGROUP_STATUS_BUDGET_EXCEED" }).label,
  ).toBe("预算不足")
  expect(statusLabel({ secondary_status: "AD_STATUS_AUDIT_DENY" }).label).toBe(
    "未通过",
  )
  // 账户状态：目录存储带前缀与不带前缀两种形式
  expect(statusLabel({ account_status: "ENABLE" }).label).toBe("正常")
  expect(statusLabel({ account_status: "STATUS_ENABLE" }).label).toBe("正常")
})

// 真实报表中的部分通过包含 REVIEW，不能被泛化成“审核中”。
test("部分通过和审核通过优先于审核中，暂停仍优先", () => {
  for (const prefix of ["AD", "ADGROUP", "CAMPAIGN"]) {
    expect(
      statusLabel({
        operation_status: "ENABLE",
        secondary_status: `${prefix}_STATUS_REVIEW_PARTIALLY_APPROVED`,
      }).label,
    ).toBe("部分审核通过")
    expect(
      statusLabel({ secondary_status: `${prefix}_STATUS_REVIEW_APPROVED` })
        .label,
    ).toBe("审核通过")
    expect(
      statusLabel({ secondary_status: `${prefix}_STATUS_REVIEW_PENDING` })
        .label,
    ).toBe("审核中")
  }
  expect(
    statusLabel({
      operation_status: "DISABLE",
      secondary_status: "AD_STATUS_REVIEW_PARTIALLY_APPROVED",
    }).label,
  ).toBe("已暂停")
})
