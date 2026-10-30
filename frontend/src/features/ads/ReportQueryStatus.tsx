import { LoaderCircle } from "lucide-react"
import { useEffect, useState } from "react"
import { Alert, AlertDescription } from "@/components/ui/alert"

export function ReportQueryStatus({
  loading,
  hasPreviousData,
}: {
  loading: boolean
  hasPreviousData: boolean
}) {
  const [slow, setSlow] = useState(false)
  useEffect(() => {
    setSlow(false)
    if (!loading) return
    // 仅对实际请求计时；提示耗时，不虚构进度或判定请求失败。
    const timer = window.setTimeout(() => setSlow(true), 8000)
    return () => window.clearTimeout(timer)
  }, [loading])

  if (!loading) return null
  return (
    <Alert role="status" aria-live="polite">
      <LoaderCircle className="animate-spin" aria-hidden="true" />
      <AlertDescription>
        <span>
          {slow ? "查询耗时较长，仍在加载，请稍候…" : "正在查询广告报表…"}
        </span>
        {hasPreviousData && (
          <span>下方暂时显示上一次查询结果，完成后自动更新。</span>
        )}
      </AlertDescription>
    </Alert>
  )
}
