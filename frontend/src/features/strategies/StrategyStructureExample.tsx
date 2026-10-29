import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { StrategiesService, type StrategyConfig_Input } from "@/client"
import { DEFAULT_COPY_POOL } from "./queries"
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

type GenerationMode = "FIXED" | "BY_MATERIAL"
type Allocation = "SHARED" | "SEQUENTIAL_AVERAGE"

export function StrategyStructureExample({
  tenantId,
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
  tenantId: string
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
  const config: StrategyConfig_Input = {
    budget,
    currency,
    budget_strategy: budgetStrategy,
    bid_strategy: bidStrategy,
    target_roas: bidStrategy === "TARGET_ROAS" ? targetRoas || null : null,
    copy_pool_version: DEFAULT_COPY_POOL,
    group_generation_mode: groupGenerationMode,
    group_count: groupGenerationMode === "FIXED" ? groupCount : null,
    group_material_allocation:
      groupGenerationMode === "FIXED" ? groupMaterialAllocation : null,
    max_materials_per_group:
      groupGenerationMode === "BY_MATERIAL" ? maxMaterialsPerGroup : null,
    ad_generation_mode: adGenerationMode,
    ads_per_group: adGenerationMode === "FIXED" ? adsPerGroup : null,
    ad_material_allocation:
      adGenerationMode === "FIXED" ? adMaterialAllocation : null,
    max_materials_per_ad:
      adGenerationMode === "BY_MATERIAL" ? maxMaterialsPerAd : null,
    creative_count: creativeCount,
  }
  const estimate = useQuery({
    queryKey: ["tenant", tenantId, "structure-example", config],
    queryFn: async ({ signal }) =>
      (
        await StrategiesService.structureEstimate({
          path: { tenant_id: tenantId },
          body: { config, unit_counts: [23], account_count: 1 },
          signal,
        })
      ).data!,
    retry: false,
    enabled:
      !!budget &&
      !!currency &&
      (bidStrategy !== "TARGET_ROAS" || !!targetRoas) &&
      creativeCount > 0 &&
      (groupGenerationMode === "FIXED"
        ? !!groupCount
        : !!maxMaterialsPerGroup) &&
      (adGenerationMode === "FIXED" ? !!adsPerGroup : !!maxMaterialsPerAd),
  })
  const allocations = estimate.data?.dramas[0] ?? []
  const groups = allocations.map((ads) =>
    adGenerationMode === "FIXED" && adMaterialAllocation === "SHARED"
      ? ads[0]
      : ads.reduce((sum, n) => sum + n, 0),
  )
  const adCounts = allocations.map((ads) => ads.length)
  const finalAdCount = estimate.data?.ad_count ?? 0
  const valid = !!estimate.data && !estimate.isError
  return (
    <Card role="region" aria-label="结构与预算示例">
      <CardHeader>
        <CardTitle>结构与预算示例</CardTitle>
        <CardDescription>
          示例由服务器按 23 个固定顺序创意单元试算；真实结果以搭建预览为准。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {!valid ? (
          <p className="text-sm text-muted-foreground">
            {estimate.isError
              ? "当前结构无法生成，请核对数量与分配规则。"
              : "填写有效数量后显示服务器结构试算。"}
          </p>
        ) : (
          <>
            <p>{estimate.data?.campaign_count} 个系列</p>
            <p>{groups.length} 个广告组</p>
            <p>
              {adCounts.length
                ? `每组 ${adCounts.join("、")} 个广告`
                : "暂无广告"}
            </p>
            <p>
              每基础广告文案版本数 K={creativeCount}，最终 {finalAdCount} 个广告
            </p>
            <p className="text-sm text-muted-foreground">
              {adGenerationMode === "BY_MATERIAL"
                ? "按素材数量生成"
                : "固定数量生成"}
              ；素材分配按当前规则执行。
            </p>
            <div className="flex flex-col gap-2">
              {groups.map((count, i) => {
                const materialCounts = allocations[i]
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
                        {count} 个创意单元 · {adCounts[i]} 个广告
                      </span>
                    </Button>
                    {expanded === i && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {materialCounts.slice(0, 100).map((n, ad) => (
                          <Badge key={ad} variant="secondary">
                            广告 {ad + 1} · {n} 个创意单元
                          </Badge>
                        ))}
                        <p className="w-full text-xs text-muted-foreground">
                          每个基础广告的完整创意单元在文案复制后保持不变。
                        </p>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </>
        )}
        {valid && (
          <p>
            示例日预算合计：{currency}{" "}
            {normalizeDecimal(estimate.data!.daily_budget_sum)}
          </p>
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
