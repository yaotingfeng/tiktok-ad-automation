import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { inputIssueText } from "./dataIssues"

export function SkippedInputs({ items }: { items: Record<string, string>[] }) {
  if (!items.length) return null
  return (
    <Alert>
      <AlertTitle>部分数据未参与判断</AlertTitle>
      <AlertDescription className="break-all">
        {items.map((item) => (
          <p key={`${item.advertiser_id}:${item.reason}`}>
            {inputIssueText(item)}
          </p>
        ))}
      </AlertDescription>
    </Alert>
  )
}
