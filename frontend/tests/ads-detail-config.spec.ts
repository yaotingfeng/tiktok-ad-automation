import { expect, test } from "@playwright/test"
import { configSections } from "../src/features/ads/details/configFields"

const flatten = (sections: ReturnType<typeof configSections>) =>
  Object.fromEntries(
    sections.flatMap((s) => s.items.map((i) => [i.label, i.value])),
  )

test("广告组配置翻译关键字段并保留未知字段", () => {
  const sections = configSections("adgroup", {
    roas_bid: "1.1",
    bid_type: "BID_TYPE_NO_BID",
    optimization_goal: "VALUE",
    vbo_window: "ZERO_DAY",
    placements: ["PLACEMENT_TIKTOK"],
    targeting_spec: {
      gender: "GENDER_UNLIMITED",
      age_groups: ["AGE_18_24", "AGE_25_34"],
      location_ids: ["6252001"],
    },
    click_attribution_window: "SEVEN_DAYS",
    brand_new_field: "x",
  })
  const flat = flatten(sections)
  expect(flat["目标 ROAS"]).toBe("1.1")
  expect(flat["出价方式"]).toBe("最大化投放")
  expect(flat["优化目标"]).toBe("价值")
  expect(flat["价值优化窗口"]).toBe("第 0 天")
  expect(flat["版位"]).toBe("TikTok")
  expect(flat["性别"]).toBe("不限")
  expect(flat["年龄"]).toBe("18-24、25-34")
  expect(flat["点击归因窗口"]).toBe("7 天")
  expect(sections.at(-1)?.title).toBe("其他字段")
  expect(flat.brand_new_field).toBe("x")
})

test("定向未映射子项以原字段名追加到版位与定向", () => {
  const sections = configSections("adgroup", {
    targeting_spec: {
      gender: "GENDER_MALE",
      brand_safety_type: "STANDARD_INVENTORY",
    },
  })
  const targeting = sections.find((s) => s.title === "版位与定向")
  expect(targeting?.items).toEqual([
    { label: "性别", value: "男" },
    { label: "brand_safety_type", value: "STANDARD_INVENTORY" },
  ])
  expect(sections.some((s) => s.title === "其他字段")).toBe(false)
})

test("广告配置与创意列表展示子项，不丢字段", () => {
  const flat = flatten(
    configSections("ad", {
      ad_text_list: [{ ad_text: "好剧" }],
      creative_list: [
        {
          creative_info: {
            ad_format: "SINGLE_VIDEO",
            video_info: { file_name: "ep1.mp4", video_id: "v1" },
          },
        },
      ],
      ad_configuration: { identity_type: "CUSTOMIZED_USER", foo: "bar" },
    }),
  )
  expect(flat["文案"]).toBe("好剧")
  expect(flat["创意列表"]).toBe("单视频：ep1.mp4")
  expect(flat["身份类型"]).toBe("CUSTOMIZED_USER")
  expect(flat.foo).toBe("bar")
})
