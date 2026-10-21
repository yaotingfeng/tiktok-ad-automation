import type {
  CopyPoolPublic,
  StrategyConfig_Output,
  StrategyPublic,
  VersionPublic,
} from "@/client"
import { demoUuid, isoDaysAgo } from "../runtime/clock.ts"
import { DEMO_STRATEGIES, DEMO_USER } from "./world.ts"

// 投放策略私有数据：完整的策略配置、版本历史、文案池。

/** 预置文案池 ID，与 queries.ts 保持一致 */
export const DEFAULT_COPY_POOL_ID = "02a4e656-a330-40dc-864c-26e81961f3ca"

/** 通用英文文案池 */
export const demoCopyPool: CopyPoolPublic = {
  id: DEFAULT_COPY_POOL_ID,
  name: "通用英文文案池 v1",
  entries: [
    { id: demoUuid(5001), text: "Watch the story continue.", position: 1 },
    { id: demoUuid(5002), text: "Don't miss the next episode!", position: 2 },
    { id: demoUuid(5003), text: "The drama unfolds here.", position: 3 },
    { id: demoUuid(5004), text: "Your next obsession awaits.", position: 4 },
    { id: demoUuid(5005), text: "Binge it now!", position: 5 },
    { id: demoUuid(5006), text: "Start watching for free.", position: 6 },
    { id: demoUuid(5007), text: "New episodes weekly.", position: 7 },
    {
      id: demoUuid(5008),
      text: "The story everyone's talking about.",
      position: 8,
    },
  ],
}

// 四个预置策略的完整配置（id/version_id/name 来自 world.ts DEMO_STRATEGIES）
const baseConfig: Omit<
  StrategyConfig_Output,
  | "budget"
  | "bid_strategy"
  | "target_roas"
  | "targeting"
  | "group_generation_mode"
  | "campaign_name_template"
> = {
  currency: "USD",
  budget_strategy: "SERIES",
  creation_status: "ENABLE",
  schedule_type: "SCHEDULE_FROM_NOW",
  schedule_start_time: null,
  schedule_end_time: null,
  group_count: null,
  group_material_allocation: null,
  max_materials_per_group: 10,
  ad_generation_mode: "FIXED",
  ads_per_group: 1,
  ad_material_allocation: "SHARED",
  max_materials_per_ad: null,
  creative_count: 2,
  copy_pool_version: DEFAULT_COPY_POOL_ID,
  cta_option_ids: [],
}

// 策略1：美区 · 目标 ROAS 1.1 · 按素材分组
const strategy1Configs: StrategyConfig_Output[] = [
  {
    ...baseConfig,
    budget: "150.00",
    bid_strategy: "TARGET_ROAS",
    target_roas: "1.1",
    targeting: {
      region_mode: "SELECTED",
      region_codes: ["US"],
      languages: ["en"],
      age_groups: ["AGE_25_34", "AGE_35_44"],
      gender: "GENDER_UNLIMITED",
    },
    group_generation_mode: "BY_MATERIAL",
    campaign_name_template: "{provider_drama}-{drama_id}",
  },
  // v2：提高了预算和 ROAS
  {
    ...baseConfig,
    budget: "180.00",
    bid_strategy: "TARGET_ROAS",
    target_roas: "1.15",
    targeting: {
      region_mode: "SELECTED",
      region_codes: ["US"],
      languages: ["en"],
      age_groups: ["AGE_25_34", "AGE_35_44"],
      gender: "GENDER_UNLIMITED",
    },
    group_generation_mode: "BY_MATERIAL",
    campaign_name_template: "{provider_drama}-{drama_id}",
  },
  // v3：当前版本，调回预算但保留 ROAS
  {
    ...baseConfig,
    budget: "160.00",
    bid_strategy: "TARGET_ROAS",
    target_roas: "1.12",
    targeting: {
      region_mode: "SELECTED",
      region_codes: ["US"],
      languages: ["en"],
      age_groups: ["AGE_25_34", "AGE_35_44"],
      gender: "GENDER_UNLIMITED",
    },
    group_generation_mode: "BY_MATERIAL",
    campaign_name_template: "{provider_drama}-{drama_id}",
  },
]

// 策略2：美区 · 最大转化 · 日预算 200
const strategy2Configs: StrategyConfig_Output[] = [
  {
    ...baseConfig,
    budget: "200.00",
    bid_strategy: "HIGHEST_VALUE",
    target_roas: null,
    targeting: {
      region_mode: "SELECTED",
      region_codes: ["US"],
      languages: ["en"],
      age_groups: [],
      gender: "GENDER_UNLIMITED",
    },
    group_generation_mode: "FIXED",
    group_count: 3,
    group_material_allocation: "SEQUENTIAL_AVERAGE",
    campaign_name_template: "{provider_drama}-{drama_id}-{YYYYMMDD}",
  },
  // v2：当前版本，改为按素材分组
  {
    ...baseConfig,
    budget: "200.00",
    bid_strategy: "HIGHEST_VALUE",
    target_roas: null,
    targeting: {
      region_mode: "SELECTED",
      region_codes: ["US"],
      languages: ["en"],
      age_groups: [],
      gender: "GENDER_UNLIMITED",
    },
    group_generation_mode: "BY_MATERIAL",
    max_materials_per_group: 8,
    campaign_name_template: "{provider_drama}-{drama_id}-{YYYYMMDD}",
  },
]

// 策略3：欧洲 · 目标 ROAS 1.3 · 系列预算
const strategy3Configs: StrategyConfig_Output[] = [
  {
    ...baseConfig,
    budget: "120.00",
    budget_strategy: "SERIES",
    bid_strategy: "TARGET_ROAS",
    target_roas: "1.3",
    targeting: {
      region_mode: "SELECTED",
      region_codes: ["GB", "DE", "FR", "IT", "ES"],
      languages: ["en", "de", "fr", "it", "es"],
      age_groups: ["AGE_18_24", "AGE_25_34"],
      gender: "GENDER_UNLIMITED",
    },
    group_generation_mode: "BY_MATERIAL",
    campaign_name_template: "{provider_drama}-{drama_id}",
  },
  // v2：扩大受众范围
  {
    ...baseConfig,
    budget: "130.00",
    budget_strategy: "SERIES",
    bid_strategy: "TARGET_ROAS",
    target_roas: "1.3",
    targeting: {
      region_mode: "SELECTED",
      region_codes: ["GB", "DE", "FR", "IT", "ES", "NL", "SE"],
      languages: ["en", "de", "fr", "it", "es", "nl", "sv"],
      age_groups: ["AGE_18_24", "AGE_25_34", "AGE_35_44"],
      gender: "GENDER_UNLIMITED",
    },
    group_generation_mode: "BY_MATERIAL",
    campaign_name_template: "{provider_drama}-{drama_id}",
  },
]

// 策略4：东南亚 · 低价拉新测试
const strategy4Configs: StrategyConfig_Output[] = [
  {
    ...baseConfig,
    budget: "80.00",
    budget_strategy: "ADGROUP",
    bid_strategy: "HIGHEST_VALUE",
    target_roas: null,
    targeting: {
      region_mode: "SELECTED",
      region_codes: ["TH", "VN", "ID", "PH"],
      languages: ["th", "vi", "id", "en"],
      age_groups: ["AGE_18_24"],
      gender: "GENDER_UNLIMITED",
    },
    group_generation_mode: "FIXED",
    group_count: 2,
    group_material_allocation: "SHARED",
    max_materials_per_group: 6,
    campaign_name_template: "{provider_drama}-{drama_id}",
  },
  // v2：当前版本，提高预算并改为按素材分组
  {
    ...baseConfig,
    budget: "100.00",
    budget_strategy: "ADGROUP",
    bid_strategy: "HIGHEST_VALUE",
    target_roas: null,
    targeting: {
      region_mode: "SELECTED",
      region_codes: ["TH", "VN", "ID", "PH", "MY"],
      languages: ["th", "vi", "id", "en", "ms"],
      age_groups: ["AGE_18_24", "AGE_25_34"],
      gender: "GENDER_UNLIMITED",
    },
    group_generation_mode: "BY_MATERIAL",
    campaign_name_template: "{provider_drama}-{drama_id}",
  },
]

// 所有策略配置的列表
const allStrategyConfigs = [
  strategy1Configs,
  strategy2Configs,
  strategy3Configs,
  strategy4Configs,
]

/** 完整的策略记录（包含历史版本） */
export interface DemoStrategyRecord {
  record: StrategyPublic
  versions: VersionPublic[]
}

/** 预置的四个策略及其历史版本 */
export const demoStrategyRecords: DemoStrategyRecord[] = DEMO_STRATEGIES.map(
  (strategy, index) => {
    const configs = allStrategyConfigs[index]
    const latestVersion = configs.length
    const versions: VersionPublic[] = configs.map((config, versionIndex) => {
      const versionNumber = versionIndex + 1
      return {
        id:
          versionNumber === latestVersion
            ? strategy.version_id
            : demoUuid(451 + index * 10 + versionIndex),
        strategy_id: strategy.id,
        number: versionNumber,
        config,
        created_by: DEMO_USER.id,
        created_at: isoDaysAgo(30 - versionIndex * 5, 10),
        request_id: demoUuid(6001 + index * 10 + versionIndex),
      }
    })

    return {
      record: {
        id: strategy.id,
        name: strategy.name,
        active: true,
        latest_version: latestVersion,
        version_id: strategy.version_id,
        config: configs[latestVersion - 1],
        created_by: DEMO_USER.id,
        created_at: versions[0].created_at,
      },
      versions,
    }
  },
)

/** 运行时新建的策略记录，支持新建策略流程 */
export const demoCreatedStrategies: DemoStrategyRecord[] = []
