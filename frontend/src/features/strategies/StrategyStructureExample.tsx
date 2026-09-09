import { useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { normalizeDecimal } from "@/features/strategies/validation"
export function StrategyStructureExample({
  groupSize,
  creativeCount,
  budget,
  currency,
}: {
  groupSize: number
  creativeCount: number
  budget: string
  currency: string
}) {
  const [expanded, setExpanded] = useState<number | null>(null)
  const valid =
    Number.isSafeInteger(groupSize) &&
    groupSize > 0 &&
    Number.isSafeInteger(creativeCount) &&
    creativeCount > 0 &&
    Number.isSafeInteger(Math.ceil(23 / groupSize) * creativeCount)
  const groups = valid ? Math.ceil(23 / groupSize) : null
  return (
    <Card role="region" aria-label="结构与预算示例">
      <CardHeader>
        <CardTitle>结构与预算示例</CardTitle>
        <CardDescription>示例：1 部剧 × 1 个账户，23 条素材</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {groups === null ? (
          <p>填写有效的每组素材数量和创意数量后显示结构。</p>
        ) : (
          <>
            <p>1 个 Campaign</p>
            <p>{groups} 个 Ad Group</p>
            <p>{groups * creativeCount} 条 Ad</p>
            <p className="text-sm text-muted-foreground">
              每组 SP1～SP{creativeCount}：相同素材，不同文案
            </p>
            <div className="flex flex-col gap-2">
              {Array.from({ length: groups }, (_, i) => (
                <div key={i}>
                  <Button
                    variant="outline"
                    className="w-full justify-between"
                    onClick={() => setExpanded(expanded === i ? null : i)}
                    aria-expanded={expanded === i}
                  >
                    素材组 {i + 1}
                    <span>
                      {Math.min(groupSize, 23 - i * groupSize)} 条素材
                    </span>
                  </Button>
                  {expanded === i && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {Array.from(
                        { length: Math.min(creativeCount, 100) },
                        (_, n) => (
                          <Badge key={n} variant="secondary">
                            SP{n + 1}
                          </Badge>
                        ),
                      )}
                      <p className="w-full text-xs text-muted-foreground">
                        共用本组素材；实际文案在搭建预览中抽取。
                        {creativeCount > 100 && "这里只展开前 100 条示例。"}
                      </p>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
        <p>
          {budget && currency
            ? `${currency} ${normalizeDecimal(budget)} / Campaign / 天`
            : "填写 Campaign 日预算和币种后显示金额。"}
        </p>
        <p className="text-sm text-muted-foreground">
          多个广告组共享该 Campaign 的日预算。
        </p>
      </CardContent>
    </Card>
  )
}
