import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { renderSuffix } from "./validation"
export const EXAMPLE_BASE = "{b30008/s328302/c3}-The Bond"
export function StrategyNamingExample({ suffix }: { suffix: string }) {
  const rendered = renderSuffix(suffix),
    campaign = rendered === null ? null : EXAMPLE_BASE + rendered
  return (
    <Card role="region" aria-label="广告命名示例">
      <CardHeader>
        <CardTitle>广告命名示例</CardTitle>
        <CardDescription>版权方归因基础名（示例），不可编辑</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <p className="break-all font-mono">{EXAMPLE_BASE}</p>
        {campaign ? (
          <dl className="flex flex-col gap-2">
            {[
              ["Campaign", campaign],
              ["Ad Group", `${campaign}-g01`],
              ["Ad", `${campaign}-g01-sp1`],
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="break-all font-mono">{value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p>修正后缀后显示名称，不截断归因基础名。</p>
        )}
        <p className="text-muted-foreground">
          示例日期与批次号仅用于说明；实际名称和接口长度在搭建预览核验。
        </p>
      </CardContent>
    </Card>
  )
}
