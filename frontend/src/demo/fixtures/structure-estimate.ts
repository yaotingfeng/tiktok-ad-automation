import type {
  StructureEstimateRequest,
  StructureEstimatePublic,
} from "@/client"

// 仅体验版的合成数据边界；正式页面从服务器规划器读取结果。
export function demoStructureEstimate(
  body: StructureEstimateRequest,
): StructureEstimatePublic {
  const c = body.config
  const sizes = (
    n: number,
    mode: string,
    fixed: number | null | undefined,
    shared: boolean,
    maximum: number | null | undefined,
  ) => {
    if (mode === "FIXED") {
      if (!fixed || fixed > 100 || (!shared && fixed > n))
        throw new Error("invalid structure")
      if (!n) return []
      return Array.from({ length: fixed }, (_, i) =>
        shared ? n : Math.floor(n / fixed) + (i < n % fixed ? 1 : 0),
      )
    }
    if (!maximum || maximum < 1 || n > 100000)
      throw new Error("invalid structure")
    return Array.from({ length: Math.ceil(n / maximum) }, (_, i) =>
      Math.min(maximum, n - i * maximum),
    )
  }
  const dramas = body.unit_counts.map((n) =>
    sizes(
      n,
      c.group_generation_mode ?? "FIXED",
      c.group_count,
      c.group_material_allocation === "SHARED",
      c.max_materials_per_group,
    ).map((n) =>
      sizes(
        n,
        c.ad_generation_mode ?? "BY_MATERIAL",
        c.ads_per_group,
        c.ad_material_allocation === "SHARED",
        c.max_materials_per_ad,
      ),
    ),
  )
  if (dramas.some((groups) => groups.some((ads) => ads.some((n) => n > 50))))
    throw new Error("invalid structure")
  const accounts = body.account_count ?? 1
  const campaign_count =
    dramas.filter((groups) => groups.length).length * accounts
  const adgroup_count =
    dramas.reduce((sum, groups) => sum + groups.length, 0) * accounts
  const ad_count =
    dramas.reduce(
      (sum, groups) => sum + groups.reduce((n, ads) => n + ads.length, 0),
      0,
    ) *
    accounts *
    (c.creative_count ?? 1)
  if (ad_count > 10000) throw new Error("batch too large")
  const [whole, fraction = ""] = String(c.budget).split(".")
  const amount =
    BigInt(whole + fraction) *
    BigInt(c.budget_strategy === "ADGROUP" ? adgroup_count : campaign_count)
  const raw = amount.toString().padStart(fraction.length + 1, "0")
  const daily_budget_sum = fraction.length
    ? `${raw.slice(0, -fraction.length)}.${raw.slice(-fraction.length)}`
    : raw
  return { dramas, campaign_count, adgroup_count, ad_count, daily_budget_sum }
}
