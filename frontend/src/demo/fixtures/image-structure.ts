// 纯演示结构常量：不依赖任何有初始化副作用的状态夹具。
export const IMAGE_DEMO_STRUCTURE = {
  group_generation_mode: "FIXED",
  group_count: 1,
  group_material_allocation: "SHARED",
  max_materials_per_group: null,
  ad_generation_mode: "BY_MATERIAL",
  max_materials_per_ad: 6,
  ads_per_group: null,
  ad_material_allocation: null,
  creative_count: 1,
} as const
