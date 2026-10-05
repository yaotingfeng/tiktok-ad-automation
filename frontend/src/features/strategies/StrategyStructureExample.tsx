import { useMemo, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  MAX_FIXED_ADS_PER_GROUP,
  MAX_FIXED_GROUP_COUNT,
  normalizeDecimal,
} from "@/features/strategies/validation"

type GenerationMode = "FIXED" | "BY_MATERIAL"
type Allocation = "SHARED" | "SEQUENTIAL_AVERAGE"

export function StrategyStructureExample({
  groupGenerationMode,
  groupCount,
  groupMaterialAllocation,
  maxMaterialsPerGroup,
  adGenerationMode,
  adsPerGroup,
  adMaterialAllocation,
  maxMaterialsPerAd,
  creativeCount,
  budget,
  currency,
  budgetStrategy,
  bidStrategy,
  targetRoas,
}: {
  groupGenerationMode: GenerationMode
  groupCount: number | null
  groupMaterialAllocation: Allocation | null
  maxMaterialsPerGroup: number | null
  adGenerationMode: GenerationMode
  adsPerGroup: number | null
  adMaterialAllocation: Allocation | null
  maxMaterialsPerAd: number | null
  creativeCount: number
  budget: string
  currency: string
  budgetStrategy: "SERIES" | "ADGROUP"
  bidStrategy: "HIGHEST_VALUE" | "TARGET_ROAS"
  targetRoas: string
}) {
  const [expanded, setExpanded] = useState<number | null>(null)
  const materialCount = 23
  // 示例组件也可能被测试或其他页面直接传入数值，不能只依赖表单层校验。
  const safeGroupCount =
    typeof groupCount === "number" &&
    Number.isSafeInteger(groupCount) &&
    groupCount > 0 &&
    groupCount <= MAX_FIXED_GROUP_COUNT
      ? groupCount
      : null
  const safeAdsPerGroup =
    typeof adsPerGroup === "number" &&
    Number.isSafeInteger(adsPerGroup) &&
    adsPerGroup > 0 &&
    adsPerGroup <= MAX_FIXED_ADS_PER_GROUP
      ? adsPerGroup
      : null
  const groups = useMemo(() => {
    if (groupGenerationMode === "FIXED") {
      if (safeGroupCount === null) return []
      if (groupMaterialAllocation === "SHARED")
        return Array.from({ length: safeGroupCount }, () => materialCount)
      const base = Math.floor(materialCount / safeGroupCount)
      const remainder = materialCount % safeGroupCount
      return Array.from(
        { length: safeGroupCount },
        (_, i) => base + (i < remainder ? 1 : 0),
      )
    }
    if (!maxMaterialsPerGroup || maxMaterialsPerGroup < 1) return []
    return Array.from(
      { length: Math.ceil(materialCount / maxMaterialsPerGroup) },
      (_, i) =>
        Math.min(
          maxMaterialsPerGroup,
          materialCount - i * maxMaterialsPerGroup,
        ),
    )
  }, [
    safeGroupCount,
    groupGenerationMode,
    groupMaterialAllocation,
    maxMaterialsPerGroup,
  ])
  const adCounts = groups.map((count) => {
    if (adGenerationMode === "FIXED") return safeAdsPerGroup || 0
    return maxMaterialsPerAd && maxMaterialsPerAd > 0
      ? Math.ceil(count / maxMaterialsPerAd)
      : 0
  })
  const adMaterialCounts = (groupMaterialCount: number) => {
    if (adGenerationMode === "FIXED") {
      const count = safeAdsPerGroup || 0
      if (adMaterialAllocation === "SHARED")
        return Array.from({ length: count }, () => groupMaterialCount)
      const base = count ? Math.floor(groupMaterialCount / count) : 0
      const remainder = count ? groupMaterialCount % count : 0
      return Array.from(
        { length: count },
        (_, i) => base + (i < remainder ? 1 : 0),
      )
    }
    const limit = maxMaterialsPerAd || 0
    return limit
      ? Array.from({ length: Math.ceil(groupMaterialCount / limit) }, (_, i) =>
          Math.min(limit, groupMaterialCount - i * limit),
        )
      : []
  }
  const baseAdCount = adCounts.reduce((sum, count) => sum + count, 0)
  const finalAdCount = baseAdCount * (creativeCount > 0 ? creativeCount : 1)
  const valid = groups.length > 0 && baseAdCount > 0 && creativeCount > 0
  return (
    <Card role="region" aria-label="结构与预算示例">
      <CardHeader>
        <CardTitle>结构与预算示例</CardTitle>
        <CardDescription>
          示例使用 23 条固定顺序素材；真实结果以搭建预览为准。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {!valid ? (
          <p className="text-sm text-muted-foreground">
            填写有效数量后显示结构示例。
          </p>
        ) : (
          <>
            <p>1 个系列</p>
            <p>{groups.length} 个广告组</p>
            <p>
              {adCounts.length
                ? `每组 ${adCounts.join("、")} 个广告`
                : "暂无广告"}
            </p>
            <p>
              创意数量 {creativeCount}，最终 {finalAdCount} 个广告
            </p>
            <p className="text-sm text-muted-foreground">
              {adGenerationMode === "BY_MATERIAL"
                ? "按素材数量生成"
                : "固定数量生成"}
              ；素材分配按当前规则执行。
            </p>
            <div className="flex flex-col gap-2">
              {groups.map((count, i) => {
                const materialCounts = adMaterialCounts(count)
                return (
                  <div key={i}>
                    <Button
                      variant="outline"
                      className="w-full justify-between"
                      onClick={() => setExpanded(expanded === i ? null : i)}
                      aria-expanded={expanded === i}
                    >
                      广告组 {i + 1}
                      <span>
                        {count} 条素材 · {adCounts[i]} 个广告
                      </span>
                    </Button>
                    {expanded === i && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {materialCounts.slice(0, 100).map((n, ad) => (
                          <Badge key={ad} variant="secondary">
                            广告 {ad + 1} · {n} 条素材
                          </Badge>
                        ))}
                        <p className="w-full text-xs text-muted-foreground">
                          每个基础广告的素材在创意复制后保持不变。
                        </p>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </>
        )}
        <p className="wrap-anywhere">
          {budget && currency
            ? `${currency} ${normalizeDecimal(budget)} / ${budgetStrategy === "ADGROUP" ? "广告组" : "系列"} / 天`
            : "填写日预算和币种后显示金额。"}
        </p>
        <p className="text-sm text-muted-foreground">
          预算：{budgetStrategy === "ADGROUP" ? "组预算" : "系列预算"}；竞价：
          {bidStrategy === "TARGET_ROAS"
            ? `目标 ROAS ${targetRoas || "待填写"}`
            : "最高价值"}
          。
        </p>
      </CardContent>
    </Card>
  )
}
