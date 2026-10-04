import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
export function ReportCoverageNotice({
  coverage,
}: {
  coverage?: Record<string, unknown>
}) {
  if (!coverage) return null
  const status = String(coverage.status ?? coverage.state ?? "").toUpperCase()
  const missing = coverage.missing_dates ?? coverage.missing_range
  if (!status && !missing) return null
  const messageByStatus: Record<string, string> = {
    AVAILABLE: "当前筛选范围已完整覆盖。",
    COMPLETE: "当前筛选范围已完整覆盖。",
    COMPLETE_EMPTY: "当前筛选范围没有可用数据。",
    EMPTY: "当前筛选范围暂无已发布数据。",
    MISSING: "目录数据缺失，目标 ROAS 可能无法显示。",
    UNAVAILABLE: "覆盖数据暂不可用。",
    UNSUPPORTED: "平台不支持该指标。",
    FAILED: "覆盖数据读取失败，请刷新重试。",
    INCOMPLETE: "报表覆盖不完整，部分日期或维度可能缺失。",
    PENDING: "报表已排队，当前显示最近一次已发布数据。",
    QUEUED: "报表正在排队，当前显示最近一次已发布数据。",
    RUNNING: "报表正在同步，当前显示最近一次已发布数据，部分范围可能缺失。",
    WAITING_REMOTE: "报表等待平台返回，当前显示最近一次已发布数据。",
  }
  const message =
    messageByStatus[status] ??
    (missing
      ? `缺失范围：${String(missing)}`
      : "报表覆盖状态尚未确认，部分范围可能缺失。")
  return (
    <Alert
      variant={
        ["AVAILABLE", "COMPLETE", "COMPLETE_EMPTY"].includes(status)
          ? "default"
          : "destructive"
      }
    >
      <AlertTitle>数据覆盖</AlertTitle>
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  )
}
