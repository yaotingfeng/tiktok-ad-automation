import { BusinessService } from "@/client"

export type BusinessOptions = {
  end_date: string
  accounts: {
    id: string
    name: string
    currency: string
    timezone: string
    end_date?: string
  }[]
  providers: { id: string; name: string }[]
}
export const businessKey = (tenantId: string) =>
  ["tenant", tenantId, "business"] as const
export const rateKey = (tenantId: string, kind: string, scopeId: string) =>
  [...businessKey(tenantId), "rate", kind, scopeId] as const

export function shiftedDay(day: string, amount: number) {
  const value = new Date(`${day}T12:00:00Z`)
  value.setUTCDate(value.getUTCDate() + amount)
  return value.toISOString().slice(0, 10)
}

export function formatValue(
  value: string | number | null | undefined,
  ratio = false,
) {
  if (value == null) return "—"
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: ratio ? 4 : 2,
    maximumFractionDigits: ratio ? 4 : 2,
  })
}

export function percentageRate(value: string) {
  const [whole, fraction = ""] = value.split(".")
  const precision = fraction.length + 2
  const digits = BigInt(`${whole || "0"}${fraction}`)
    .toString()
    .padStart(precision + 1, "0")
  return `${digits.slice(0, -precision)}.${digits.slice(-precision)}`
    .replace(/0+$/, "")
    .replace(/\.$/, "")
}

export const optionsQuery = (tenantId: string, bcId: string) => ({
  queryKey: [...businessKey(tenantId), "options", bcId],
  queryFn: async ({ signal }: { signal: AbortSignal }) =>
    (
      await BusinessService.options({
        path: { tenant_id: tenantId },
        query: { bc_id: bcId },
        signal,
      })
    ).data as unknown as BusinessOptions,
})

export const ISSUE_LABELS: Record<string, string> = {
  PROVIDER_RATE_MISSING:
    "部分日期的版权方抽成待配置，请在对应版权方的抽成设置中补齐；相关经营金额暂不核算",
  REBATE_RATE_MISSING:
    "部分日期的其他返点待配置，请在经营设置中补齐时间段；相关经营金额暂不核算",
  DRAMA_UNMAPPED:
    "部分系列的版权方标签无对应连接或存在歧义，相关经营收入暂不核算",
  DRAMA_IDENTITY_CONFLICT:
    "部分系列在期间内出现不同剧目身份，相关补贴暂留空，请核对系列归属",
  DRAMA_FROM_REPORT_NAME:
    "部分历史剧目按广告报表命名识别；没有可靠剧编号的分组暂不预计新剧补贴",
  COMPENSATION_PARTIAL:
    "预计赔付仅展示已计算小计，仍有日期或系列缺少依据，不能视为完整总额。",
  COMPENSATION_QUALIFICATION_PENDING:
    "预计赔付已有条件预计数值；余额、风控、素材审核及平台最终资格仍待核实。",
  COMPENSATION_MANUAL_CONFLICT:
    "人工记录未绑定当前自动依据，已保留审计但未覆盖本次计算，请刷新依据后更正。",
  COMPENSATION_EVIDENCE_MISSING:
    "部分日期的日志、实际配置或D0日报依据不足，相关预计留空；可查看系列赔付依据了解缺口",
  SUBSIDY_SCOPE_INCOMPLETE:
    "补贴所需季度历史、币种或归属信息未齐，相关预计金额暂留空",
  SUBSIDY_POLICY_UNCONFIGURED: "所选日期尚未配置补贴政策，预计补贴暂不可计算",
  SOURCE_INCOMPLETE: "所选日期存在日报缺口，金额为已核算小计",
  POLICY_SCOPE_RESTRICTED: "补贴依赖范围包含未授权账户，预计补贴暂不展示",
}

export const EVIDENCE_REASON_LABELS: Record<string, string> = {
  LOG_COVERAGE_MISSING: "该日操作日志尚未完整同步",
  CONFIGURATION_ANCHOR_MISSING: "缺少实际配置观察，暂不能还原历史出价",
  LOG_CHAIN_INCOMPLETE: "配置观察与核算日之间仍有日志缺口",
  BID_CHAIN_AMBIGUOUS: "改价顺序或前后值无法唯一还原",
  BID_VALUE_UNKNOWN: "日志中的ROAS值无法识别",
  BID_LOG_UNSUPPORTED: "日志中的出价层级暂无法核验",
  STRUCTURE_UNSUPPORTED: "目前仅核算CBO Smart+ Minis D0 IAA目标ROAS结构",
  OBJECT_RELATION_UNKNOWN: "部分广告组缺少可靠的系列归属",
  OPERATION_FIELD_UNKNOWN: "部分操作字段无法核验",
  STATUS_VALUE_UNKNOWN: "状态变更内容不完整或无法识别",
  BUDGET_VALUE_UNKNOWN: "预算变更内容不完整或单位无法核验",
  ADGROUP_MIN_BUDGET_CHANGED: "当日调整过广告组最低预算，按规则不预计赔付",
  BUDGET_DECREASED: "当日降低过预算，按规则不预计赔付",
  CAMPAIGN_PAUSED: "当日暂停过系列，恢复开启不消除该日排除条件",
  CAMPAIGN_DELETED: "当日删除过系列，按规则不预计赔付",
  MANUAL_BID_LIMIT: "当日已确认的人工改价超过两次",
  NUMERIC_INPUT_MISSING: "缺少完整的消耗、D0收入、IAA事件或当日最低出价",
  THRESHOLD_NOT_MET: "未达到赔付触发条件",
  PLATFORM_QUALIFICATION_PENDING:
    "已有条件预计，余额、风控、素材审核和平台资格仍待核实",
  BID_SOURCE_UNKNOWN: "部分改价来源无法确认是否为人工操作",
}
