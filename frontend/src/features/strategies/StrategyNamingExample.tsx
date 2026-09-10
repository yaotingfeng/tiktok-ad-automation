import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { renderDefaultName, renderSuffix } from "./validation"
export const EXAMPLE_BASE = "{b30008/s328302/c3}-The Bond"
export function StrategyNamingExample({
  suffix,
  nameTemplate,
}: {
  suffix: string
  nameTemplate: string
}) {
  const rendered = renderSuffix(suffix)
  const examples = [
    { label: "嘉书 · 默认规则", campaign: renderDefaultName(nameTemplate) },
    {
      label: "网眼 · 专用规则",
      campaign: rendered === null ? null : EXAMPLE_BASE + rendered,
    },
  ]
  return (
    <Card role="region" aria-label="广告命名示例">
      <CardHeader>
        <CardTitle>广告命名示例</CardTitle>
        <CardDescription>专用归因规则优先，其余使用默认模板。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5 text-sm">
        {examples.map(({ label, campaign }) => (
          <section
            key={label}
            aria-label={label}
            className="flex flex-col gap-2"
          >
            <p className="font-medium">{label}</p>
            {campaign ? (
              <dl className="flex flex-col gap-2">
                {[
                  ["Campaign", campaign],
                  ["Ad Group", `${campaign}-g01`],
                  ["Ad", `${campaign}-g01-sp1`],
                ].map(([level, value]) => (
                  <div key={level}>
                    <dt className="text-muted-foreground">{level}</dt>
                    <dd className="break-all font-mono">{value}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p>修正对应模板后显示名称。</p>
            )}
          </section>
        ))}
        <p className="text-muted-foreground">
          示例日期与随机号仅用于说明。同一搭建批次复用随机号；实际名称和长度在搭建预览核验。
        </p>
      </CardContent>
    </Card>
  )
}
