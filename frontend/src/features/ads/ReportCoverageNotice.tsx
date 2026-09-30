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
  const message =
    status === "COMPLETE"
      ? "当前筛选范围已完整覆盖。"
      : missing
        ? `缺失范围：${String(missing)}`
        : "报表仍在同步，部分范围可能缺失。"
  return (
    <Alert variant={status === "COMPLETE" ? "default" : "destructive"}>
      <AlertTitle>数据覆盖</AlertTitle>
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  )
}
