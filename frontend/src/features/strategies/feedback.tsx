import { AxiosError } from "axios"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { RequestError } from "@/features/tenants/shared"
import { issueMessages } from "./validation"

const messages: Record<string, string> = {
  ...issueMessages,
  strategy_not_found: "当前租户找不到此策略、版本或保存记录。",
  configuration_invalid: "当前策略配置无效，请检查各字段。",
  invalid_cursor: "分页位置已失效，请返回第一页后重试。",
}
export function StrategyError({
  error,
  retry,
}: {
  error: unknown
  retry?: () => void
}) {
  const code =
    error instanceof AxiosError ? error.response?.data?.code : undefined
  if (!messages[code]) return <RequestError error={error} retry={retry} />
  return (
    <Alert variant="destructive">
      <AlertTitle>策略请求未完成</AlertTitle>
      <AlertDescription>
        <p>{messages[code]}</p>
        {retry && (
          <Button variant="outline" size="sm" onClick={retry}>
            重试
          </Button>
        )}
      </AlertDescription>
    </Alert>
  )
}
