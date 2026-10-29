import type { CarouselCreativeIdentity, FrozenCarouselLink } from "@/client"
import { dateDaysAgo, demoUuid, isoDaysAgo } from "../runtime/clock"

// 体验版共享“演示世界”：各领域的假后端都从这里取租户、BC、账户、剧、素材和已上线广告，
// 保证同一个 BC、同一批账户和剧贯穿所有页面。所有名称、ID、金额均为虚构。
// 各领域把这些中性数据映射成自己接口的类型；领域私有数据放在各自的 fixtures 文件里。

/** 0–1 的确定性伪随机数，同一个种子总得到同一个值，保证报表刷新后数字稳定。 */
export function seeded(seed: string | number) {
  let h = 2166136261
  for (const ch of String(seed)) {
    h ^= ch.charCodeAt(0)
    h = Math.imul(h, 16777619)
  }
  h ^= h >>> 13
  h = Math.imul(h, 0x5bd1e995)
  h ^= h >>> 15
  return (h >>> 0) / 4294967296
}

export const DEMO_TOKEN = "demo-access-token"

export const DEMO_USER = {
  id: demoUuid(1),
  username: "demo",
  full_name: "体验账号",
  is_active: true,
  is_superuser: true,
  created_at: isoDaysAgo(120),
} as const

/** 其他演示成员，用于成员管理、操作人展示。 */
export const DEMO_TEAM = [
  {
    id: demoUuid(2),
    username: "lin.yue",
    full_name: "林悦",
    role: "tenant_admin",
  },
  {
    id: demoUuid(3),
    username: "zhou.hang",
    full_name: "周航",
    role: "operator",
  },
  { id: demoUuid(4), username: "chen.xi", full_name: "陈曦", role: "operator" },
  { id: demoUuid(5), username: "wang.lei", full_name: "王磊", role: "viewer" },
] as const

export const DEMO_TENANTS = [
  {
    id: demoUuid(101),
    name: "星河短剧（演示）",
    active: true,
    role: "platform_admin",
    default_bc_id: "7400000000000000101",
  },
  {
    id: demoUuid(102),
    name: "海鸥出海（演示）",
    active: true,
    role: "tenant_admin",
    default_bc_id: "7400000000000000201",
  },
] as const
export const MAIN_TENANT_ID = DEMO_TENANTS[0].id

export const DEMO_BCS = [
  {
    bc_id: "7400000000000000101",
    tenant_id: MAIN_TENANT_ID,
    name: "星河短剧 BC-A",
    currency: "USD",
    timezone: "Asia/Shanghai",
  },
  {
    bc_id: "7400000000000000102",
    tenant_id: MAIN_TENANT_ID,
    name: "星河短剧 BC-B",
    currency: "USD",
    timezone: "Asia/Shanghai",
  },
  {
    bc_id: "7400000000000000201",
    tenant_id: DEMO_TENANTS[1].id,
    name: "海鸥出海 BC",
    currency: "USD",
    timezone: "Asia/Shanghai",
  },
] as const
export const MAIN_BC_ID = DEMO_BCS[0].bc_id

export interface DemoAdvertiser {
  advertiser_id: string
  bc_id: string
  name: string
  currency: string
  timezone: string
  /** 投放账户可以建广告；素材账户只用于上传素材。 */
  purpose: "delivery" | "material"
  balance: number
}

const advertiserNames = [
  "XH-美区-01",
  "XH-美区-02",
  "XH-美区-03",
  "XH-欧洲-01",
  "XH-东南亚-01",
  "XH-中东-01",
  "XH-素材库-01",
  "XH-素材库-02",
]
export const DEMO_ADVERTISERS: DemoAdvertiser[] = [
  ...advertiserNames.map((name, index) => ({
    advertiser_id: `74310000000000${(1001 + index).toString().padStart(5, "0")}`,
    bc_id: MAIN_BC_ID,
    name,
    currency: "USD",
    timezone: "Asia/Shanghai",
    purpose: (name.includes("素材") ? "material" : "delivery") as
      | "delivery"
      | "material",
    balance: Math.round(2000 + seeded(name) * 18000),
  })),
  ...["XHB-美区-01", "XHB-美区-02"].map((name, index) => ({
    advertiser_id: `74310000000000${(2001 + index).toString().padStart(5, "0")}`,
    bc_id: DEMO_BCS[1].bc_id,
    name,
    currency: "USD",
    timezone: "Asia/Shanghai",
    purpose: "delivery" as const,
    balance: Math.round(3000 + seeded(name) * 9000),
  })),
  ...["HO-美区-01", "HO-素材库-01"].map((name, index) => ({
    advertiser_id: `74310000000000${(3001 + index).toString().padStart(5, "0")}`,
    bc_id: DEMO_BCS[2].bc_id,
    name,
    currency: "USD",
    timezone: "Asia/Shanghai",
    purpose: (name.includes("素材") ? "material" : "delivery") as
      | "delivery"
      | "material",
    balance: Math.round(1500 + seeded(name) * 6000),
  })),
]
export const advertisersOfBc = (bcId: string) =>
  DEMO_ADVERTISERS.filter((a) => a.bc_id === bcId)
export const deliveryAdvertisers = (bcId: string = MAIN_BC_ID) =>
  advertisersOfBc(bcId).filter((a) => a.purpose === "delivery")
export const materialAdvertisers = (bcId: string = MAIN_BC_ID) =>
  advertisersOfBc(bcId).filter((a) => a.purpose === "material")

/** TikTok 身份（广告展示的账号头像/名称）。 */
export const DEMO_IDENTITIES = [
  {
    identity_id: "7432000000000000001",
    display_name: "Velvet Reels",
    identity_type: "BC_AUTH_TT",
  },
  {
    identity_id: "7432000000000000002",
    display_name: "Moonlight Drama",
    identity_type: "BC_AUTH_TT",
  },
  {
    identity_id: "7432000000000000003",
    display_name: "StarRiver Shorts",
    identity_type: "CUSTOMIZED_USER",
  },
] as const

/** 版权方（分销商）连接；kind 与真实系统中的版权方类型一致。 */
export const DEMO_PROVIDERS = [
  { id: demoUuid(201), kind: "wangyan", display_name: "网眼 · 主账号" },
  { id: demoUuid(202), kind: "jiashu", display_name: "嘉书 · ReelBox" },
  { id: demoUuid(203), kind: "gangganhao", display_name: "刚刚好 · 分销后台" },
  { id: demoUuid(204), kind: "duiba", display_name: "兑吧 · 奇境分销" },
] as const

export interface DemoDrama {
  /** 系统内剧 ID */
  id: string
  /** 版权方剧 ID */
  external_id: string
  provider_id: string
  provider_kind: string
  title: string
  title_zh: string
  language: string
  episodes: number
  free_episodes: number
  /** 封面主色，用于生成内联 SVG 封面。 */
  color: string
  genre: string
}

const dramaSeeds: [string, string, string, string][] = [
  ["The Billionaire's Secret Bride", "总裁的秘密新娘", "#9d4edd", "豪门"],
  ["Reborn as the Alpha's Mate", "重生之狼王伴侣", "#3a86ff", "狼人"],
  [
    "My Contract Husband Is a Mafia Boss",
    "契约老公是黑帮老大",
    "#e63946",
    "黑帮",
  ],
  ["Return of the Divorced Heiress", "离婚后千金归来", "#f4a261", "复仇"],
  ["The CEO's Runaway Twins", "总裁的逃跑双胞胎", "#2a9d8f", "萌宝"],
  ["Queen of Revenge", "复仇女王", "#d62828", "复仇"],
  ["Fake Marriage, Real Love", "假婚真爱", "#ff70a6", "甜宠"],
  ["The Hidden Dragon Son-in-Law", "隐龙赘婿", "#264653", "逆袭"],
  ["Her Second Chance at Love", "她的第二次爱情", "#e76f51", "都市"],
  ["Bound to the Vampire King", "血族之王的契约", "#6a040f", "奇幻"],
  ["The Nanny Who Saved the Tycoon", "拯救富豪的保姆", "#588157", "甜宠"],
  ["Lost Princess Comes Home", "失踪公主回家了", "#ffb703", "豪门"],
  ["Divorce Me If You Dare", "有本事你离婚", "#8338ec", "都市"],
  ["Love After the Storm", "风暴之后的爱", "#219ebc", "都市"],
  ["The Doctor's Hidden Identity", "神医的隐藏身份", "#1d3557", "逆袭"],
  ["Sweet Revenge on My Ex", "甜蜜复仇前任", "#ef476f", "复仇"],
]
export const DEMO_DRAMAS: DemoDrama[] = dramaSeeds.map(
  ([title, title_zh, color, genre], index) => {
    const provider = DEMO_PROVIDERS[index % DEMO_PROVIDERS.length]
    return {
      id: demoUuid(301 + index),
      external_id: `${provider.kind.slice(0, 2).toUpperCase()}${(88100 + index * 7).toString()}`,
      provider_id: provider.id,
      provider_kind: provider.kind,
      title,
      title_zh,
      language: "en",
      episodes: 60 + Math.round(seeded(title) * 40),
      free_episodes: 5 + (index % 4),
      color,
      genre,
    }
  },
)
export const dramaById = (id: string) => DEMO_DRAMAS.find((d) => d.id === id)

/** 内联 SVG 封面（9:16），避免任何外部图片请求。 */
export function demoCover(
  drama: Pick<DemoDrama, "title" | "color">,
  label = "",
) {
  const words = drama.title.split(" ")
  const lines: string[] = []
  for (const word of words) {
    const last = lines[lines.length - 1]
    if (last && (last + word).length < 14)
      lines[lines.length - 1] = `${last} ${word}`
    else lines.push(word)
  }
  const text = lines
    .slice(0, 4)
    .map(
      (line, i) =>
        `<text x="90" y="${130 + i * 26}" font-size="20" font-family="sans-serif" font-weight="700" fill="#fff" text-anchor="middle">${line.replace(/&/g, "&amp;").replace(/'/g, "&apos;")}</text>`,
    )
    .join("")
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="180" height="320" viewBox="0 0 180 320"><defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${drama.color}"/><stop offset="1" stop-color="#111"/></linearGradient></defs><rect width="180" height="320" fill="url(#g)"/>${text}<text x="90" y="290" font-size="14" font-family="sans-serif" fill="#ffffffcc" text-anchor="middle">${label}</text></svg>`
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
}

export interface DemoMaterial {
  id: string
  drama_id: string
  /** 第几条素材（同剧内序号）。 */
  index: number
  file_name: string
  duration_seconds: number
  size_bytes: number
  /** 已上传到的素材账户与 TikTok 视频 ID。 */
  advertiser_id: string
  video_id: string
  uploaded_at: string
  cover_url: string
}

/** 每部剧预置 4–6 条已入库素材（最后 4 部剧留空，演示“上传素材”）。 */
export const DEMO_MATERIALS: DemoMaterial[] = DEMO_DRAMAS.slice(0, 12).flatMap(
  (drama, dramaIndex) => {
    const count = 4 + (dramaIndex % 3)
    return Array.from({ length: count }, (_, i) => {
      const n = dramaIndex * 10 + i
      const advertiser = materialAdvertisers()[n % materialAdvertisers().length]
      return {
        id: demoUuid(1001 + n),
        drama_id: drama.id,
        index: i + 1,
        // 真实规则：素材文件名需包含完整剧名，搭建时按剧名匹配素材。
        file_name: `${drama.title}_${String(i + 1).padStart(2, "0")}.mp4`,
        duration_seconds: 45 + Math.round(seeded(`dur${n}`) * 90),
        size_bytes: Math.round((40 + seeded(`size${n}`) * 160) * 1024 * 1024),
        advertiser_id: advertiser.advertiser_id,
        video_id: `v10044g50000${(n + 100000).toString(36)}demo`,
        uploaded_at: isoDaysAgo(3 + (n % 20), 9 + (n % 9)),
        cover_url: demoCover(drama, `素材 ${i + 1}`),
      }
    })
  },
)
export const materialsOfDrama = (dramaId: string) =>
  DEMO_MATERIALS.filter((m) => m.drama_id === dramaId)

/** 预置的投放策略（策略领域生成完整版本数据，ID 与名称以这里为准）。 */
export const DEMO_STRATEGIES = [
  {
    id: demoUuid(401),
    version_id: demoUuid(451),
    name: "美区 · 目标 ROAS 1.1 · 按素材分组",
  },
  {
    id: demoUuid(402),
    version_id: demoUuid(452),
    name: "美区 · 最大转化 · 日预算 200",
  },
  {
    id: demoUuid(403),
    version_id: demoUuid(453),
    name: "欧洲 · 目标 ROAS 1.3 · 系列预算",
  },
  {
    id: demoUuid(404),
    version_id: demoUuid(454),
    name: "东南亚 · 低价拉新测试",
  },
] as const

/** 已上线的广告结构：搭建提交后追加，广告报表与广告管理从这里读取。 */
export interface DemoLaunchedAd {
  carousel_creatives?: CarouselCreativeIdentity[]
  frozen_creative_links?: FrozenCarouselLink[]
  ad_id: string
  name: string
  material_id: string
  status: "ENABLE" | "DISABLE"
}
export interface DemoLaunchedAdGroup {
  adgroup_id: string
  name: string
  budget: number
  status: "ENABLE" | "DISABLE"
  ads: DemoLaunchedAd[]
}
export interface DemoLaunchedCampaign {
  campaign_id: string
  name: string
  advertiser_id: string
  bc_id: string
  drama_id: string
  strategy_id: string
  /** 来自哪次搭建提交（预置数据也指向预置的历史提交）。 */
  submission_id: string | null
  budget: number
  target_roas: number | null
  status: "ENABLE" | "DISABLE"
  created_at: string
  /** 投放开始距今天数，报表按它生成趋势；新上线的为 0。 */
  age_days: number
  adgroups: DemoLaunchedAdGroup[]
}

let launchedSeq = 0
function makeCampaign(
  drama: DemoDrama,
  advertiser: DemoAdvertiser,
  strategyIndex: number,
  ageDays: number,
  submissionId: string | null,
): DemoLaunchedCampaign {
  const n = ++launchedSeq
  const materials = materialsOfDrama(drama.id)
  const campaignId = `17${(8000000000000000 + n * 1031).toString()}`
  const groupCount = Math.max(1, Math.min(3, materials.length - 2))
  return {
    campaign_id: campaignId,
    name: `${drama.provider_kind}-${drama.title}-${drama.external_id}`,
    advertiser_id: advertiser.advertiser_id,
    bc_id: advertiser.bc_id,
    drama_id: drama.id,
    strategy_id: DEMO_STRATEGIES[strategyIndex % DEMO_STRATEGIES.length].id,
    submission_id: submissionId,
    budget: [100, 200, 300][n % 3],
    target_roas: strategyIndex % 2 === 0 ? 1.1 : null,
    status: seeded(`c${n}`) > 0.85 ? "DISABLE" : "ENABLE",
    created_at: isoDaysAgo(ageDays, 10),
    age_days: ageDays,
    adgroups: Array.from({ length: groupCount }, (_, g) => {
      const groupId = `18${(8000000000000000 + n * 1031 + g * 17).toString()}`
      const groupMaterials = materials.filter((_, i) => i % groupCount === g)
      return {
        adgroup_id: groupId,
        name: `${drama.external_id}-组${g + 1}`,
        budget: 50,
        status: "ENABLE" as const,
        ads: groupMaterials.map((material, a) => ({
          ad_id: `19${(8000000000000000 + n * 1031 + g * 17 + a).toString()}`,
          name: `${drama.external_id}-素材${material.index}`,
          material_id: material.id,
          status: "ENABLE" as const,
        })),
      }
    }),
  }
}

/** 历史搭建提交的固定 ID，供搭建任务页和报表互相引用。 */
export const HISTORY_SUBMISSION_IDS = [
  demoUuid(601),
  demoUuid(602),
  demoUuid(603),
] as const

function seedLaunched(): DemoLaunchedCampaign[] {
  const out: DemoLaunchedCampaign[] = []
  const delivery = deliveryAdvertisers()
  // 前 9 部剧已投放 3–28 天，分布在不同账户和三次历史提交里。
  DEMO_DRAMAS.slice(0, 9).forEach((drama, i) => {
    const accounts = [
      delivery[i % delivery.length],
      delivery[(i + 2) % delivery.length],
    ]
    for (const advertiser of accounts)
      out.push(
        makeCampaign(
          drama,
          advertiser,
          i,
          [28, 21, 14, 10, 7, 5, 4, 3, 3][i],
          HISTORY_SUBMISSION_IDS[i % 3],
        ),
      )
  })
  return out
}

export const demoWorld = {
  launched: seedLaunched(),
}

/** 搭建提交成功后调用：为每个剧 × 账户生成一个刚上线的 Campaign。 */
export function launchCampaigns(input: {
  submissionId: string
  dramaIds: string[]
  advertiserIds: string[]
  strategyId?: string
}) {
  const created: DemoLaunchedCampaign[] = []
  for (const dramaId of input.dramaIds) {
    const drama = dramaById(dramaId)
    if (!drama) continue
    for (const advertiserId of input.advertiserIds) {
      const advertiser = DEMO_ADVERTISERS.find(
        (a) => a.advertiser_id === advertiserId,
      )
      if (!advertiser) continue
      const strategyIndex = Math.max(
        0,
        DEMO_STRATEGIES.findIndex((s) => s.id === input.strategyId),
      )
      const campaign = makeCampaign(
        drama,
        advertiser,
        strategyIndex,
        0,
        input.submissionId,
      )
      campaign.status = "ENABLE"
      created.push(campaign)
    }
  }
  demoWorld.launched.unshift(...created)
  return created
}

export { dateDaysAgo }
