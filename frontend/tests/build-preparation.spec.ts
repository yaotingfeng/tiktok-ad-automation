import { expect, test } from "@playwright/test"
import {
  BC as bc,
  buildsBoundary,
  D,
  P,
  pickInputs,
  T as tenant,
} from "./utils/buildsBoundary"

test("保存的搭建可从草稿箱找回完整输入，刷新后默认回到新建", async ({
  page,
}) => {
  const api = await buildsBoundary(page, { draftList: true })
  api.summary.status = "DRAFT"
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await pickInputs(page)
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/build-drafts/${D}`))
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  const row = page.getByRole("row").filter({ hasText: "完整剧名1" })
  await expect(row).toContainText("已保存")
  await expect(row).toContainText("已输入 3 行")
  await expect(row).toContainText("已解析 0 个账户")
  await page.reload()
  await expect(page.getByLabel("剧目名称", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await row.getByRole("link", { name: "继续搭建" }).click()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    api.originals.drama.join("\n"),
  )
  await expect(page.getByLabel("广告账户", { exact: true })).toHaveValue(
    api.originals.account.join("\n"),
  )
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(1)
})

test("打开关闭草稿箱保留新建输入，加载其他草稿才提示离页", async ({ page }) => {
  const api = await buildsBoundary(page, { draftList: true })
  api.summary.status = "DRAFT"
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await expect(page.getByLabel("剧目名称", { exact: true })).toBeVisible()
  await expect(page.getByRole("tab", { name: "新建搭建" })).toHaveCount(0)
  expect(
    api.requests.filter((r) => r.path.endsWith("/build-drafts")),
  ).toHaveLength(0)
  await page.getByLabel("剧目名称", { exact: true }).fill("尚未保存的剧名")
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await expect(
    page.getByRole("dialog", { name: "草稿箱", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("dialog", { name: "有未保存的修改", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "关闭草稿箱", exact: true }).click()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    "尚未保存的剧名",
  )
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await page.keyboard.press("Escape")
  await expect(
    page.getByRole("dialog", { name: "草稿箱", exact: true }),
  ).toHaveCount(0)
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    "尚未保存的剧名",
  )
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await page.getByRole("link", { name: "继续搭建" }).click()
  await expect(
    page.getByRole("dialog", { name: "有未保存的修改", exact: true }),
  ).toBeVisible()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    "尚未保存的剧名",
  )
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await page.getByRole("link", { name: "继续搭建" }).click()
  await page.getByRole("button", { name: "丢弃未保存修改" }).click()
  await expect(page).toHaveURL(new RegExp(`/build-drafts/${D}`))
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    api.originals.drama.join("\n"),
  )
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("空草稿箱关闭后可以直接填写新建表单", async ({ page }) => {
  await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await expect(page.getByText("草稿箱为空", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "关闭草稿箱", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "草稿箱", exact: true }),
  ).toBeFocused()
  await page.getByLabel("剧目名称", { exact: true }).fill("新剧")
})

test("准备中的批次继续查看原进度，不重复发起准备", async ({ page }) => {
  const api = await buildsBoundary(page, { draftList: true })
  api.summary.status = "PREPARING"
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await page.getByRole("link", { name: "继续搭建" }).click()
  await expect(
    page.getByRole("heading", { name: "准备与调整", exact: true }),
  ).toBeVisible()
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("当前版本已有预览时继续原预览，不重新创建", async ({ page }) => {
  const api = await buildsBoundary(page, { draftList: true })
  await page.route("**/build-drafts?**", async (route) => {
    await route.fulfill({
      json: {
        items: [
          {
            ...api.summary,
            drama_titles: ["预览剧目"],
            drama_input_count: 1,
            account_input_count: 3,
            resolved_account_count: 3,
            strategy_label: "默认策略 v1",
            preview_id: P,
            preview_status: "FROZEN",
          },
        ],
        next_cursor: null,
      },
    })
  })
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await expect(
    page.getByRole("cell", { name: "待提交", exact: true }),
  ).toBeVisible()
  await page.getByRole("link", { name: "继续搭建" }).click()
  await expect(page).toHaveURL(new RegExp(`/build-previews/${P}`))
  await expect(
    page.getByRole("heading", { name: "搭建预览", exact: true }),
  ).toBeVisible()
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("只读成员可从草稿箱查看，切BC重置草稿箱", async ({ page }) => {
  const api = await buildsBoundary(page, { draftList: true, viewer: true })
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await expect(
    page.getByRole("link", { name: "查看详情", exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("link", { name: "继续搭建" })).toHaveCount(0)
  await page.getByRole("button", { name: "关闭草稿箱", exact: true }).click()
  await page.getByRole("combobox", { name: "当前 BC", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "备用 BC" }).click()
  await expect(
    page.getByRole("dialog", { name: "草稿箱", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await expect(page.getByText("草稿箱为空", { exact: true })).toBeVisible()
  await expect(page.getByText("完整剧名1、完整剧名2")).toHaveCount(0)
  expect(api.requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("草稿箱列表服务端分页，刷新回到最近修改的第一页", async ({ page }) => {
  const api = await buildsBoundary(page)
  const pages: string[] = []
  await page.route("**/build-drafts?**", async (route) => {
    const query = new URL(route.request().url()).searchParams
    pages.push(query.get("cursor") || "first")
    expect(query.get("bc_id")).toBe(bc)
    expect(query.get("limit")).toBe("50")
    const next = !!query.get("cursor")
    await route.fulfill({
      json: {
        items: [
          {
            ...api.summary,
            drama_titles: [next ? "第二页剧目" : "最新剧目"],
            drama_input_count: 1,
            account_input_count: 3,
            resolved_account_count: 0,
            strategy_label: "默认策略 v1",
            preview_id: null,
            preview_status: null,
          },
        ],
        next_cursor: next ? null : "opaque-page-two",
      },
    })
  })
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await expect(page.getByText("最新剧目", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "下一页" }).click()
  await expect(page.getByText("第二页剧目", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "刷新列表" }).click()
  await expect(page.getByText("最新剧目", { exact: true })).toBeVisible()
  expect(pages).toEqual(["first", "opaque-page-two", "first"])
})

test("草稿箱列表刷新被拒绝时隐藏旧记录", async ({ page }) => {
  await buildsBoundary(page, { draftList: true })
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await page.getByRole("button", { name: "草稿箱", exact: true }).click()
  await expect(page.getByText("完整剧名1、完整剧名2")).toBeVisible()
  await page.route("**/build-drafts?**", (route) =>
    route.fulfill({ status: 403, json: { code: "action_forbidden" } }),
  )
  await page.getByRole("button", { name: "刷新列表" }).click()
  await expect(page.getByText("无权访问此页面")).toBeVisible()
  await expect(page.getByText("完整剧名1、完整剧名2")).toHaveCount(0)
  await expect(page.getByRole("link", { name: "继续搭建" })).toHaveCount(0)
})

for (const width of [390, 1440]) {
  test(`草稿箱列表在${width}宽度保持页内布局`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    const api = await buildsBoundary(page, { draftList: true })
    api.originals.drama = [
      "等待确认的剧目甲",
      "等待确认的剧目乙",
      "等待确认的剧目丙",
    ]
    api.summary.status = "BLOCKED"
    await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
    await expect(page.getByLabel("剧目名称", { exact: true })).toBeVisible()
    await page.screenshot({
      path: testInfo.outputPath(`build-entry-${width}.png`),
      animations: "disabled",
    })
    await page.getByRole("button", { name: "草稿箱", exact: true }).click()
    await expect(
      page.getByRole("cell", { name: "待处理", exact: true }),
    ).toBeVisible()
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true)
    await page.screenshot({
      path: testInfo.outputPath(`draft-box-${width}.png`),
      animations: "disabled",
    })
  })
}

test("输入页保持剧目和账户批量原文，离开守卫取消保留输入", async ({ page }) => {
  await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await page
    .getByLabel("剧目名称", { exact: true })
    .fill("完整剧名甲\n完整剧名乙")
  await page
    .getByLabel("广告账户", { exact: true })
    .fill("90071992547409939999\n账户乙")
  await expect(page.getByText("全部剧目将覆盖同一批全部有效账户")).toBeVisible()
  await page.getByRole("link", { name: "素材库", exact: true }).click()
  await expect(
    page.getByRole("dialog", { name: "有未保存的修改" }),
  ).toBeVisible()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    "完整剧名甲\n完整剧名乙",
  )
  await expect(page.getByLabel("广告账户", { exact: true })).toHaveValue(
    "90071992547409939999\n账户乙",
  )
})
test("两剧三账户原文保存后准备，应用ID不等于MinisID", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await pickInputs(page)
  await page.getByRole("button", { name: "解析并准备", exact: true }).click()
  await expect(
    page.getByRole("heading", { name: "准备与调整", exact: true }),
  ).toBeVisible()
  await expect
    .poll(
      () =>
        api.requests.filter(
          (r) => r.method === "POST" && r.path.endsWith("/prepare"),
        ).length,
    )
    .toBe(1)
  const create = api.requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/build-drafts"),
  )!.body
  expect(create.account_lines).toEqual([
    "90071992547409936666",
    "广告户乙",
    "广告户丙",
  ])
  expect(create.drama_lines).toHaveLength(2)
  expect(create.application_id).toBe("app-external-001")
  expect(create.bc_id).toBe(bc)
})
test("刷新草稿编辑完整恢复超过100行后才能保存", async ({ page }) => {
  const api = await buildsBoundary(page, { inputCount: 151 })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}&edit=true`)
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    api.originals.drama.join("\n"),
  )
  expect(
    api.requests
      .filter(
        (r) => r.path.endsWith("/inputs") && r.query.get("kind") === "drama",
      )
      .map((r) => r.query.get("cursor")),
  ).toEqual([null, "100"])
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect(
    page.getByRole("heading", { name: "准备与调整", exact: true }),
  ).toBeVisible()
  expect(api.requests.filter((r) => r.method === "PATCH")).toHaveLength(0)
})
test("未知创建只查原请求，404不开放新建", async ({ page }) => {
  const api = await buildsBoundary(page, {
    createUnknown: true,
    lookup404: true,
  })
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await pickInputs(page)
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await page.getByRole("button", { name: "查询原保存结果" }).click()
  await expect(
    page.getByRole("button", { name: "保存草稿", exact: true }),
  ).toBeDisabled()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    "完整剧名1\n完整剧名2",
  )
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/build-drafts"),
    ),
  ).toHaveLength(1)
})

test("不可用应用不可选择，选择器不会触发草稿保存", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await page.getByRole("combobox", { name: "版权方连接", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "已验证连接" }).click()
  await page.getByRole("combobox", { name: "推广应用", exact: true }).click()
  await expect(
    page.getByRole("option").filter({ hasText: "禁用应用" }),
  ).toBeDisabled()
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(0)
})
test("取消长输入恢复不暴露空编辑表单，重新恢复读完全部页", async ({ page }) => {
  const api = await buildsBoundary(page, {
    inputCount: 151,
    delayedInput: true,
  })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}&edit=true`)
  await expect(
    page.getByText("正在恢复完整原输入，已加载 100 行…", { exact: true }),
  ).toBeVisible()
  await page.getByRole("button", { name: "取消加载", exact: true }).click()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveCount(0)
  api.releaseInput()
  await page.getByRole("button", { name: "重新加载完整输入" }).click()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    api.originals.drama.join("\n"),
  )
  expect(api.requests.filter((r) => r.method === "PATCH")).toHaveLength(0)
})
test("打开素材即可编辑，移除尾组保存其余完整素材", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "查看与调整素材" }).first().click()
  await expect(
    page.getByText("此素材也匹配了其他剧目", { exact: false }).first(),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存素材分组" }),
  ).toBeDisabled()
  for (const n of [21, 22, 23])
    await page
      .getByRole("button", { name: `移除 完整剧名1-${n}.mp4`, exact: true })
      .click()
  await page.getByRole("button", { name: "保存素材分组" }).click()
  await expect(page.getByRole("dialog", { name: /调整素材/ })).toHaveCount(0)
  const body = api.requests.find(
    (r) => r.method === "PATCH" && r.path.endsWith("/groups"),
  )!.body
  expect(body.groups.map((g: string[]) => g.length)).toEqual([10, 10])
  expect(body.expected_revision).toBe(1)
})
test("素材未保存关闭取消保留组号，确认仅离开不写API", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "查看与调整素材" }).first().click()
  await page.getByLabel("组号", { exact: true }).first().fill("5")
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(page.getByLabel("组号", { exact: true }).first()).toHaveValue(
    "5",
  )
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await page.getByRole("button", { name: "丢弃未保存修改" }).click()
  expect(api.requests.filter((r) => r.method === "PATCH")).toHaveLength(0)
})
test("未知准备只回查原请求不再次POST，原输入继续可读", async ({ page }) => {
  const api = await buildsBoundary(page, {
    prepareUnknown: true,
    lookup404: true,
  })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "解析并准备", exact: true }).click()
  await page.getByRole("button", { name: "查询原准备结果" }).click()
  await expect(
    page.getByRole("button", { name: "解析并准备", exact: true }),
  ).toBeDisabled()
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/prepare"),
    ),
  ).toHaveLength(1)
})
test("只读角色能看准备结果和分组，不能启动准备或编辑", async ({ page }) => {
  await buildsBoundary(page, { viewer: true })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await expect(
    page.getByRole("button", { name: "解析并准备", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "查看与调整素材" }).first().click()
  await expect(
    page.getByRole("button", { name: "开始调整完整分组" }),
  ).toHaveCount(0)
})
test("草稿403保留登录并显示权限反馈", async ({ page }) => {
  await buildsBoundary(page, { deny: true })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await expect(page.getByText("无权访问此页面", { exact: true })).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "build-test-token",
  )
})

test("直接打开带prepare参数的URL不会自动发起版权方操作", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(
    `/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}&prepare=true`,
  )
  await expect(
    page.getByRole("heading", { name: "准备与调整", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "解析并准备", exact: true }),
  ).toBeEnabled()
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(0)
})
test("未知创建刷新仍保留原输入与原请求ID", async ({ page }) => {
  const api = await buildsBoundary(page, {
    createUnknown: true,
    lookup404: true,
  })
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await pickInputs(page)
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "查询原保存结果" }),
  ).toBeVisible()
  await page.reload()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    "完整剧名1\n完整剧名2",
  )
  await page.getByRole("button", { name: "查询原保存结果" }).click()
  const create = api.requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/build-drafts"),
  )!
  expect(
    api.requests.some((r) => r.path.endsWith(create.body.request_id)),
  ).toBe(true)
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(1)
})

test("编辑草稿切BC守卫取消保留原文，确认后进入新BC输入页", async ({ page }) => {
  const { BC2 } = await import("./utils/buildsBoundary"),
    api = await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}&edit=true`)
  await page.getByLabel("剧目名称", { exact: true }).fill("不能丢失的剧名")
  await page.getByRole("combobox", { name: "当前 BC", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "备用 BC" }).click()
  await page.getByRole("button", { name: "留在当前页" }).click()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    "不能丢失的剧名",
  )
  await page.getByRole("combobox", { name: "当前 BC", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "备用 BC" }).click()
  await page.getByRole("button", { name: "丢弃未保存修改" }).click()
  await expect(page).toHaveURL(new RegExp(`/builds/new\\?bc_id=${BC2}`))
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue("")
  expect(api.requests.filter((r) => r.method === "PATCH")).toHaveLength(0)
})
test("恢复输入期间切租户取消原请求且不请求新租户旧草稿", async ({ page }) => {
  const { T2 } = await import("./utils/buildsBoundary"),
    api = await buildsBoundary(page, { inputCount: 151, delayedInput: true })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}&edit=true`)
  await expect(
    page.getByText("正在恢复完整原输入，已加载 100 行…", { exact: true }),
  ).toBeVisible()
  await page.getByRole("combobox", { name: "当前租户", exact: true }).click()
  await page.getByRole("option").filter({ hasText: "搭建租户乙" }).click()
  await expect(page).toHaveURL(new RegExp(`/tenants/${T2}/builds/new`))
  api.releaseInput()
  expect(
    api.requests.some((r) =>
      r.path.includes(`/tenants/${T2}/build-drafts/${D}`),
    ),
  ).toBe(false)
})
test("草稿保存409保留全部本地输入", async ({ page }) => {
  await buildsBoundary(page, { conflict: true })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}&edit=true`)
  await page
    .getByLabel("剧目名称", { exact: true })
    .fill("本地编辑甲\n本地编辑乙")
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect(page.getByText("草稿已更新", { exact: true })).toBeVisible()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    "本地编辑甲\n本地编辑乙",
  )
})

test("未知输入修改刷新后只按原request_id回查，不再次PATCH", async ({
  page,
}) => {
  const api = await buildsBoundary(page, { updateUnknown: true })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}&edit=true`)
  await page.getByLabel("剧目名称", { exact: true }).fill("保留这一份原修改")
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "查询原修改结果" }),
  ).toBeVisible()
  await page.reload()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    "保留这一份原修改",
  )
  await page.getByRole("button", { name: "查询原修改结果" }).click()
  await expect(
    page.getByRole("heading", { name: "准备与调整", exact: true }),
  ).toBeVisible()
  const patches = api.requests.filter((r) => r.method === "PATCH")
  expect(patches).toHaveLength(1)
  expect(patches[0].body.request_id).toMatch(/^[\da-f-]{36}$/)
  expect(
    api.requests.some(
      (r) =>
        r.method === "GET" &&
        r.path.endsWith(
          `/build-mutation-requests/${patches[0].body.request_id}`,
        ),
    ),
  ).toBe(true)
})
test("未知分组修改只查原请求，404保留编辑且阻止再次保存", async ({ page }) => {
  const api = await buildsBoundary(page, {
    groupUnknown: true,
    lookup404: true,
  })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "查看与调整素材" }).first().click()
  await page.getByLabel("组号", { exact: true }).first().fill("7")
  await page.getByRole("button", { name: "保存素材分组" }).click()
  await page.getByRole("button", { name: "查询原修改结果" }).click()
  await expect(page.getByLabel("组号", { exact: true }).first()).toHaveValue(
    "7",
  )
  await expect(
    page.getByRole("button", { name: "保存素材分组" }),
  ).toBeDisabled()
  expect(api.requests.filter((r) => r.method === "PATCH")).toHaveLength(1)
})

test("后台草稿GET失败不卸载正在编辑的原文，403变为只读并保留登录", async ({
  page,
}) => {
  await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}&edit=true`)
  await page
    .getByLabel("剧目名称", { exact: true })
    .fill("后台检查失败也保留的输入")
  await page.route(`**/api/tenants/${tenant}/build-drafts/${D}`, (route) =>
    route.fulfill({ status: 503, json: { code: "service_unavailable" } }),
  )
  await page.getByRole("button", { name: "检查最新草稿状态" }).click()
  await expect(page.getByText("请求未完成", { exact: true })).toBeVisible({
    timeout: 10000,
  })
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    "后台检查失败也保留的输入",
  )
  await page.route(`**/api/tenants/${tenant}/build-drafts/${D}`, (route) =>
    route.fulfill({ status: 403, json: { code: "action_forbidden" } }),
  )
  await page.getByRole("button", { name: "检查最新草稿状态" }).click()
  await expect(page.getByLabel("剧目名称", { exact: true })).toBeDisabled()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue(
    "后台检查失败也保留的输入",
  )
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "build-test-token",
  )
})

test("合并剧目列表按50/100服务端分页", async ({ page }) => {
  const api = await buildsBoundary(page, { inputCount: 151 })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await expect(
    page.getByRole("button", { name: "输入详情", exact: true }),
  ).toHaveCount(50)
  await expect(
    page.getByRole("tab", { name: "剧目输入", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect
    .poll(
      () =>
        api.requests.filter(
          (r) => r.path.endsWith("/inputs") && r.query.get("cursor") === "50",
        ).length,
    )
    .toBe(1)
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "输入详情", exact: true }),
  ).toHaveCount(100)
})

test("版权方候选只提交对应输入ID，并恢复本地准备同步结果", async ({ page }) => {
  const api = await buildsBoundary(page, { candidate: "drama" })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "选择剧目", exact: true }).click()
  await page.getByRole("button").filter({ hasText: "候选正式剧名" }).click()
  await expect
    .poll(
      () =>
        api.requests.filter(
          (r) => r.path.endsWith("/candidate") && r.method === "POST",
        ).length,
    )
    .toBe(1)
  const candidate = api.requests.find((r) => r.path.endsWith("/candidate"))!
  expect(candidate.body).toEqual({ external_drama_id: "external-drama-01" })
  expect(candidate.path).toContain(
    "/inputs/44444444-4444-4444-8444-444444444444/candidate",
  )
  await expect
    .poll(
      () =>
        api.requests.filter(
          (r) => r.path.endsWith("/prepare") && r.method === "POST",
        ).length,
    )
    .toBe(1)
  await expect(
    page.getByRole("button", { name: "选择剧目", exact: true }),
  ).toHaveCount(0)
  await page.reload()
  await expect(
    page.getByRole("button", { name: "输入详情", exact: true }),
  ).toHaveCount(2)
  await expect(
    page.getByRole("button", { name: "选择剧目", exact: true }),
  ).toHaveCount(0)
})
test("账户歧义展示完整ID并返回原输入修正，不调用版权方候选API", async ({
  page,
}) => {
  const api = await buildsBoundary(page, { candidate: "account" })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("tab", { name: "账户解析", exact: true }).click()
  await page.getByRole("button", { name: "查看账户候选", exact: true }).click()
  await page
    .getByRole("button", {
      name: /复制账户 ID 并返回修正.*90071992547409937777/,
    })
    .click()
  await expect(page.getByLabel("广告账户", { exact: true })).toHaveValue(
    api.originals.account.join("\n"),
  )
  expect(
    api.requests.filter((r) => r.path.endsWith("/candidate")),
  ).toHaveLength(0)
})

test("未知候选选择关闭重开也不允许重复或改选", async ({ page }) => {
  const api = await buildsBoundary(page, { candidate: "drama" })
  await page.route("**/api/**/candidate", (route) => route.abort("failed"))
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "选择剧目", exact: true }).click()
  await page.getByRole("button").filter({ hasText: "候选正式剧名" }).click()
  await expect(
    page.getByText("选择结果尚待核实，正在刷新状态，请勿重复选择。", {
      exact: true,
    }),
  ).toBeVisible()
  await expect(
    page.getByRole("button").filter({ hasText: "候选正式剧名" }),
  ).toBeDisabled()
  await page.keyboard.press("Escape")
  await page.getByRole("button", { name: "选择剧目", exact: true }).click()
  await expect(
    page.getByRole("button").filter({ hasText: "候选正式剧名" }),
  ).toBeDisabled()
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/prepare"),
    ),
  ).toHaveLength(0)
})

for (const viewer of [false, true]) {
  test(`已有租户无BC仅引导当前账户页（${viewer ? "viewer" : "operator"}）`, async ({
    page,
  }) => {
    const api = await buildsBoundary(page, { viewer })
    await page.route("**/api/tenants/*/bcs*", (route) =>
      route.fulfill({ json: { items: [], next_cursor: null } }),
    )
    await page.goto(`/tenants/${tenant}/builds/new`)
    await expect(
      page.getByRole("heading", { name: "尚未连接 TikTok BC", exact: true }),
    ).toBeVisible()
    await expect(page.getByText("尚未接入租户", { exact: true })).toHaveCount(0)
    await expect(
      page.getByText(/当前尚无租户和 BC|请联系平台管理员/),
    ).toHaveCount(0)
    const link = page.getByRole("link", { name: "查看账户与授权", exact: true })
    await expect(link).toHaveAttribute(
      "href",
      `/tenants/${tenant}/accounts?tab=connections`,
    )
    await expect(
      page.getByText(/请联系租户管理员完成 TikTok 授权/),
    ).toBeVisible()
    expect(
      api.requests.filter((request) => request.method === "POST"),
    ).toHaveLength(0)
  })
}

test("没有版权方 Mini 配置时按名称选择并自动继续准备", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "选择小程序", exact: true }).click()
  const dialog = page.getByRole("dialog", { name: "选择推广小程序" })
  await expect(dialog.getByText("LemonShow", { exact: true })).toBeVisible()
  await expect(dialog.getByRole("textbox")).toHaveCount(0)
  await dialog.getByRole("button", { name: /LemonShow/ }).click()
  await expect(dialog).toHaveCount(0)
  await expect
    .poll(
      () =>
        api.requests.filter(
          (r) => r.path.endsWith("/prepare") && r.method === "POST",
        ).length,
    )
    .toBe(0)
  const selection = api.requests.find(
    (r) => r.path.endsWith("/minis") && r.method === "POST",
  )!
  expect(selection.body.minis_id).toBe("mini-real-001")
  expect(selection.body.expected_revision).toBe(1)
  expect(selection.body.catalog_job_id).toBeTruthy()
  expect(
    api.requests.some(
      (r) => r.path.includes("/applications/") && r.method === "PATCH",
    ),
  ).toBe(false)
  await page.screenshot({ path: "../.runtime/auto-minis/mini-selection.png" })
})

test("小程序保存响应丢失后查询原请求并继续，不重复保存", async ({ page }) => {
  const api = await buildsBoundary(page, { miniUnknown: true })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "选择小程序", exact: true }).click()
  await page
    .getByRole("dialog", { name: "选择推广小程序" })
    .getByRole("button", { name: /LemonShow/ })
    .click()
  await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible()
  await page.keyboard.press("Escape")
  await expect(
    page.getByRole("button", { name: "查询原修改结果" }),
  ).toBeVisible()
  await page.reload()
  await page.getByRole("button", { name: "查询原修改结果" }).click()
  await expect
    .poll(
      () =>
        api.requests.filter(
          (r) => r.method === "POST" && r.path.endsWith("/prepare"),
        ).length,
    )
    .toBe(0)
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/minis"),
    ),
  ).toHaveLength(1)
})

test("只读成员可看小程序名称但不能选择", async ({ page }) => {
  await buildsBoundary(page, { viewer: true })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await expect(page.getByText("推广小程序", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "选择小程序", exact: true }),
  ).toHaveCount(0)
})

test("剧目先展示输入，慢速取链与完成状态原位更新，刷新不插入提示或清空表格", async ({
  page,
}) => {
  const api = await buildsBoundary(page)
  api.summary.status = "DRAFT"
  api.summary.drama_count = 0
  let stage: "input" | "link" | "ready" = "input"
  let hold = false
  let release: () => void = () => {}
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route(`**/build-drafts/${D}/prepare`, async (route) => {
    api.summary.status = "PREPARING"
    await route.fulfill({ status: 202, json: { task_id: D, revision: 1 } })
  })
  await page.route(`**/build-drafts/${D}/inputs?**`, async (route) => {
    if (new URL(route.request().url()).searchParams.get("kind") !== "drama")
      return route.fallback()
    if (hold) await held
    await route.fulfill({
      json: {
        items: [
          {
            id: "stable-input",
            kind: "drama",
            line_no: 1,
            raw_text: "12345",
            status: stage === "ready" ? "ready" : "pending",
            reason_code: null,
            duplicate_of: null,
            advertiser_id: null,
            drama_id: null,
            provider_input_id: null,
            candidates: [],
            preparation: {
              link_status: stage === "ready" ? "ready" : "pending",
              title: stage === "input" ? null : "确认后的正式剧名",
              reason_code: null,
              drama:
                stage === "ready"
                  ? {
                      drama_id: D,
                      title: "确认后的正式剧名",
                      first_line: 1,
                      link_id: D,
                      matched_count: 12,
                      material_state: "ready",
                    }
                  : null,
            },
          },
        ],
        next_cursor: null,
      },
    })
  })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await expect(page.getByText("12345", { exact: true })).toBeVisible()
  await expect(page.getByText("等待解析", { exact: true })).toBeVisible()
  const row = page.getByRole("row").nth(1)
  const originalRow = await row.elementHandle()
  const top = (await page.getByRole("table").boundingBox())!.y
  await page.getByRole("button", { name: "解析并准备", exact: true }).click()
  await expect(page.getByText("正在解析剧目…", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "准备中…", exact: true }),
  ).toBeDisabled()
  await expect(
    page.getByRole("button", { name: "查询原准备结果" }),
  ).toHaveCount(0)
  stage = "link"
  await expect(page.getByText("正在获取链接…", { exact: true })).toBeVisible()
  await expect(
    page.getByText("确认后的正式剧名", { exact: true }),
  ).toBeVisible()
  if (process.env.BUILD_SCREENSHOT_DIR) {
    await page.screenshot({
      path: `${process.env.BUILD_SCREENSHOT_DIR}/build-link-preparing.png`,
      fullPage: true,
      animations: "disabled",
    })
  }
  hold = true
  stage = "ready"
  api.summary.status = "READY"
  api.summary.drama_count = 1
  const fetchStarted = page.waitForRequest((req) =>
    req.url().includes(`/build-drafts/${D}/inputs?`),
  )
  await page.getByRole("button", { name: "刷新准备结果" }).click()
  await fetchStarted
  await expect(
    page.getByText("确认后的正式剧名", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("正在更新列表…", { exact: true })).toHaveCount(0)
  await expect(page.locator('table [data-slot="skeleton"]')).toHaveCount(0)
  expect((await page.getByRole("table").boundingBox())!.y).toBe(top)
  release()
  await expect(
    page.getByRole("button", { name: "已获取", exact: true }),
  ).toBeVisible()
  expect(
    await row.evaluate(
      (element, previous) => element === previous,
      originalRow,
    ),
  ).toBe(true)
  await expect(page.getByRole("link", { name: /查看取链/ })).toHaveCount(0)
  await page.reload()
  await expect(
    page.getByRole("button", { name: "已获取", exact: true }),
  ).toBeVisible()
  await expect(page.getByText("12345", { exact: true })).toHaveCount(0)
})

test("取链失败、重复和结果待核实均保留原行，修改草稿后不残留旧剧目", async ({
  page,
}) => {
  const api = await buildsBoundary(page)
  api.summary.status = "PREPARING"
  let updated = false
  await page.route(`**/build-drafts/${D}/inputs?**`, async (route) => {
    await route.fulfill({
      json: {
        items: (updated
          ? ["pending"]
          : ["failed", "duplicate", "result_unknown"]
        ).map((status, i) => ({
          id: `input-${updated ? "new" : i}`,
          kind: "drama",
          line_no: i + 1,
          raw_text: updated ? "新的输入" : `原始剧目${i + 1}`,
          status,
          reason_code: status === "failed" ? "drama_not_found" : null,
          duplicate_of: status === "duplicate" ? 1 : null,
          advertiser_id: null,
          drama_id: null,
          provider_input_id: null,
          candidates: [],
          preparation: {
            link_status: status,
            title: null,
            reason_code: null,
            drama: null,
          },
        })),
        next_cursor: null,
      },
    })
  })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await expect(page.getByRole("row")).toHaveCount(4)
  await expect(page.getByText("未找到完整剧名", { exact: true })).toBeVisible()
  await expect(page.getByText("合并到第 1 行", { exact: true })).toBeVisible()
  await expect(page.getByText("结果待核实", { exact: true })).toBeVisible()
  await expect(page.getByText("尚无已确认剧目", { exact: true })).toHaveCount(0)
  await page.reload()
  await expect(page.getByText("结果待核实", { exact: true })).toBeVisible()
  updated = true
  api.summary.revision++
  api.summary.status = "DRAFT"
  await page.getByRole("button", { name: "刷新准备结果" }).click()
  await expect(page.getByText("新的输入", { exact: true })).toBeVisible()
  await expect(page.getByText(/原始剧目/)).toHaveCount(0)
  await expect(page.getByRole("row")).toHaveCount(2)
})

test("剧目列表显示版权方ID，原始输入在详情，只有待选择状态出现选择剧目", async ({
  page,
}) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await expect(
    page.getByRole("columnheader", { name: "剧目标识", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("provider-drama-1", { exact: true }),
  ).toBeVisible()
  await expect(page.getByRole("table")).not.toContainText(
    "88888888-8888-4888-8888-888888888888",
  )
  await expect(
    page.getByRole("button", { name: "选择剧目", exact: true }),
  ).toHaveCount(0)
  await page
    .getByRole("button", { name: "输入详情", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: "输入详情", exact: true })
  await expect(sheet.getByText("原始输入", { exact: true })).toBeVisible()
  await expect(
    sheet.getByText(api.originals.drama[0], { exact: true }),
  ).toHaveCount(2)
  await expect(
    sheet.getByText("provider-drama-1", { exact: true }),
  ).toBeVisible()
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(0)
})

test("只读成员可查看剧目详情但不能选择候选", async ({ page }) => {
  const api = await buildsBoundary(page, { candidate: "drama", viewer: true })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await expect(
    page.getByRole("button", { name: "选择剧目", exact: true }),
  ).toHaveCount(0)
  await page
    .getByRole("button", { name: "输入详情", exact: true })
    .first()
    .click()
  await expect(page.getByRole("dialog")).toBeVisible()
  await expect(
    page.getByRole("button").filter({ hasText: "候选正式剧名" }),
  ).toHaveCount(0)
  expect(api.requests.filter((r) => r.method === "POST")).toHaveLength(0)
})

test("其他版权方可批量添加现成链接，第一步不增加剧目表", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await pickInputs(page)
  await expect(
    page.getByRole("button", { name: "其他版权方", exact: true }),
  ).toHaveCount(0)
  await page.getByRole("combobox", { name: "版权方连接", exact: true }).click()
  const providerDialog = page.getByRole("dialog", { name: "选择版权方连接" })
  const otherProvider = providerDialog.getByRole("button", {
    name: "其他版权方",
    exact: true,
  })
  await expect(otherProvider).toBeVisible()
  await expect(
    providerDialog.getByRole("option", { selected: true }),
  ).toContainText("已验证连接")
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 })
    const bounds = (await otherProvider.boundingBox())!
    const search = (await providerDialog
      .getByLabel("搜索版权方连接")
      .boundingBox())!
    expect(bounds.x).toBeGreaterThanOrEqual(0)
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width)
    expect(bounds.y).toBeGreaterThan(search.y + search.height)
    if (process.env.BUILD_SCREENSHOT_DIR) {
      await page.screenshot({
        path: `${process.env.BUILD_SCREENSHOT_DIR}/provider-dialog-${width}.png`,
        animations: "disabled",
      })
    }
  }
  await page.route("**/providers/connections?**", async (route) => {
    await route.fulfill({
      json: {
        items: Array.from({ length: 50 }, (_, index) => ({
          id: `provider-${index}`,
          display_name: `版权方 ${index + 1}`,
          kind: "wangyan",
          status: "active",
        })),
        next_cursor: null,
      },
    })
  })
  await providerDialog.getByLabel("搜索版权方连接").fill("版权方")
  await providerDialog
    .getByRole("button", { name: "搜索", exact: true })
    .click()
  await expect(providerDialog.getByRole("option")).toHaveCount(50)
  await page.setViewportSize({ width: 390, height: 640 })
  await expect
    .poll(async () => {
      const bounds = (await otherProvider.boundingBox())!
      return bounds.y + bounds.height
    })
    .toBeLessThanOrEqual(640)
  // 视口变化后先等待布局稳定，再比较列表滚动前后的入口位置。
  await otherProvider.click({ trial: true })
  const beforeScroll = (await otherProvider.boundingBox())!
  expect(beforeScroll.y + beforeScroll.height).toBeLessThanOrEqual(640)
  await providerDialog.getByRole("option").last().scrollIntoViewIfNeeded()
  const afterScroll = (await otherProvider.boundingBox())!
  expect(Math.abs(afterScroll.y - beforeScroll.y)).toBeLessThan(1)
  if (process.env.BUILD_SCREENSHOT_DIR) {
    await page.screenshot({
      path: `${process.env.BUILD_SCREENSHOT_DIR}/provider-dialog-long-list.png`,
      animations: "disabled",
    })
  }
  await otherProvider.click()
  await expect(providerDialog).toHaveCount(0)
  await expect(
    page.getByRole("combobox", { name: "版权方连接", exact: true }),
  ).toHaveText("其他版权方")
  await expect(
    page.getByRole("button", { name: "其他版权方", exact: true }),
  ).toHaveCount(0)
  await page.getByLabel("版权方名称", { exact: true }).fill("新版权方")
  await page
    .getByRole("button", { name: "已有推广链接？批量添加", exact: true })
    .click()
  await page
    .getByLabel("剧名与推广链接", { exact: true })
    .fill(
      "新剧甲\thttps://www.tiktok.com/minis/a?channel=x%2By\n新剧乙\thttps://www.tiktok.com/minis/b",
    )
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 })
    const dialog = page.getByRole("dialog")
    const bounds = (await dialog.boundingBox())!
    expect(bounds.x).toBeGreaterThanOrEqual(0)
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width)
    if (process.env.BUILD_SCREENSHOT_DIR) {
      await page.screenshot({
        path: `${process.env.BUILD_SCREENSHOT_DIR}/manual-links-import-${width}.png`,
        fullPage: false,
        animations: "disabled",
      })
    }
  }
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.getByRole("button", { name: "添加到剧目", exact: true }).click()
  await expect(page.getByRole("table")).toHaveCount(0)
  if (process.env.BUILD_SCREENSHOT_DIR) {
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({
      path: `${process.env.BUILD_SCREENSHOT_DIR}/manual-links-input.png`,
      fullPage: true,
      animations: "disabled",
    })
  }
  await expect(
    page.getByText("其中 2 部已填写推广链接", { exact: false }),
  ).toBeVisible()
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/build-drafts/${D}`))
  const body = api.requests.find(
    (r) => r.method === "POST" && r.path.endsWith("/build-drafts"),
  )!.body
  expect(body.custom_provider_name).toBe("新版权方")
  expect(body.provider_connection_id).toBeNull()
  expect(body.manual_links).toEqual([
    {
      line_no: 3,
      url: "https://www.tiktok.com/minis/a?channel=x%2By",
      external_drama_id: "",
      protected_base: "",
    },
    {
      line_no: 4,
      url: "https://www.tiktok.com/minis/b",
      external_drama_id: "",
      protected_base: "",
    },
  ])
})

test("第二步原行补链提交对应输入，随后继续准备", async ({ page }) => {
  const api = await buildsBoundary(page, { empty: true })
  let written: any
  await page.route("**/inputs/*/manual-link", async (route) => {
    written = route.request().postDataJSON()
    expect(route.request().method()).toBe("PUT")
    expect(route.request().url()).toContain("/inputs/input-drama-0/manual-link")
    await route.fulfill({ json: { draft_id: D, revision: 2 } })
  })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page
    .getByRole("row")
    .filter({ hasText: "完整剧名1" })
    .getByRole("button", { name: "补充推广链接", exact: true })
    .click()
  await page
    .getByLabel("推广链接", { exact: true })
    .fill("https://www.tiktok.com/minis/a?channel=x%2By")
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 })
    await expect
      .poll(async () => {
        const bounds = (await page.getByRole("dialog").boundingBox())!
        return bounds.x >= 0 && bounds.x + bounds.width <= width
      })
      .toBe(true)
    if (process.env.BUILD_SCREENSHOT_DIR) {
      await page.screenshot({
        path: `${process.env.BUILD_SCREENSHOT_DIR}/manual-link-sheet-${width}.png`,
        fullPage: false,
        animations: "disabled",
      })
    }
  }
  await page
    .getByRole("button", { name: "保存并继续准备", exact: true })
    .click()
  await expect(
    page.getByRole("dialog", { name: "补充推广链接", exact: true }),
  ).toHaveCount(0)
  expect(written.expected_revision).toBe(1)
  expect(written.link).toEqual({
    line_no: 1,
    url: "https://www.tiktok.com/minis/a?channel=x%2By",
    external_drama_id: "",
    protected_base: "",
  })
  await expect
    .poll(
      () =>
        api.requests.filter(
          (r) => r.method === "POST" && r.path.endsWith("/prepare"),
        ).length,
    )
    .toBe(1)
})

test("批量链接校验失败保留输入，取消不改变原剧名", async ({ page }) => {
  await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/builds/new?bc_id=${bc}`)
  await page.getByLabel("剧目名称", { exact: true }).fill("原剧")
  await page
    .getByRole("button", { name: "已有推广链接？批量添加", exact: true })
    .click()
  await page
    .getByLabel("剧名与推广链接")
    .fill("新剧\thttps://wrong.example/link")
  await page.getByRole("button", { name: "添加到剧目", exact: true }).click()
  await expect(page.getByRole("alert")).toContainText("第 1 行")
  await expect(page.getByLabel("剧名与推广链接")).toHaveValue(
    "新剧\thttps://wrong.example/link",
  )
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await expect(page.getByLabel("剧目名称", { exact: true })).toHaveValue("原剧")
})

test("准备中和只读成员不显示补链操作", async ({ page }) => {
  const api = await buildsBoundary(page)
  api.summary.status = "PREPARING"
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await expect(
    page.getByRole("heading", { name: "准备与调整", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "补充推广链接", exact: true }),
  ).toHaveCount(0)
  await buildsBoundary(page, { viewer: true })
  await page.reload()
  await expect(
    page.getByRole("heading", { name: "准备与调整", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "补充推广链接", exact: true }),
  ).toHaveCount(0)
})

test("补链响应丢失后刷新回查原请求并继续准备", async ({ page }) => {
  const api = await buildsBoundary(page, { empty: true })
  let writes = 0
  let requestId = ""
  await page.route("**/inputs/*/manual-link", async (route) => {
    writes++
    requestId = route.request().postDataJSON().request_id
    await route.abort("failed")
  })
  await page.route("**/build-mutation-requests/*", async (route) => {
    expect(route.request().url()).toContain(requestId)
    await route.fulfill({ json: { draft_id: D, revision: 2 } })
  })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page
    .getByRole("button", { name: "补充推广链接", exact: true })
    .first()
    .click()
  await page
    .getByLabel("推广链接", { exact: true })
    .fill("https://www.tiktok.com/minis/a")
  await page
    .getByRole("button", { name: "保存并继续准备", exact: true })
    .click()
  await expect(
    page.getByRole("button", { name: "查询保存结果", exact: true }),
  ).toBeVisible()
  await page.reload()
  await page
    .getByRole("button", { name: "查询原修改结果", exact: true })
    .click()
  await expect
    .poll(
      () =>
        api.requests.filter(
          (r) => r.method === "POST" && r.path.endsWith("/prepare"),
        ).length,
    )
    .toBe(1)
  expect(writes).toBe(1)
})

test("恢复草稿保留手动链接并随剧名顺序保存", async ({ page }) => {
  const api = await buildsBoundary(page)
  api.summary.status = "DRAFT"
  await page.route("**/build-drafts/*/inputs?**", async (route) => {
    if (new URL(route.request().url()).searchParams.get("kind") !== "drama")
      return route.fallback()
    await route.fulfill({
      json: {
        items: api.originals.drama.map((raw_text, index) => ({
          id: `input-${index}`,
          kind: "drama",
          line_no: index + 1,
          raw_text,
          status: "pending",
          reason_code: null,
          duplicate_of: null,
          advertiser_id: null,
          drama_id: null,
          provider_input_id: null,
          candidates: [],
          manual_link:
            index === 0
              ? {
                  line_no: 1,
                  url: "https://www.tiktok.com/minis/original",
                  external_drama_id: "123",
                  protected_base: "",
                }
              : {},
        })),
        next_cursor: null,
      },
    })
  })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}&edit=true`)
  await expect(
    page.getByText("其中 1 部已填写推广链接", { exact: false }),
  ).toBeVisible()
  await page
    .getByLabel("剧目名称", { exact: true })
    .fill("完整剧名2\n完整剧名1")
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect
    .poll(() => api.requests.filter((r) => r.method === "PATCH").length)
    .toBe(1)
  const body = api.requests.find((r) => r.method === "PATCH")!.body
  expect(body.manual_links).toEqual([
    {
      line_no: 2,
      url: "https://www.tiktok.com/minis/original",
      external_drama_id: "123",
      protected_base: "",
    },
  ])
})

for (const width of [1440, 390]) {
  test(`素材长文件名在 ${width}px 下不挤压组号和移除操作`, async ({ page }) => {
    await buildsBoundary(page)
    const fileName = `${"这是一部名称很长的短剧素材".repeat(12)}_20260914_第001集_1080p.mp4`
    await page.route("**/build-drafts/*/dramas/*/materials?**", (route) =>
      route.fulfill({
        json: {
          items: [
            {
              material_id: "material-long-name",
              file_name: fileName,
              group_no: 1,
              position: 1,
              shared_with_other_drama: false,
            },
          ],
          next_cursor: null,
        },
      }),
    )
    await page.setViewportSize({ width, height: 900 })
    await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
    await page.getByRole("button", { name: "查看与调整素材" }).first().click()
    const dialog = page.getByRole("dialog", { name: /调整素材/ })
    const input = dialog.getByLabel("组号", { exact: true })
    const remove = dialog.getByRole("button", {
      name: `移除 ${fileName}`,
      exact: true,
    })
    await expect(input).toBeEnabled()
    await expect(remove).toHaveText("移除")
    await expect(
      page.getByRole("button", { name: "开始调整完整分组" }),
    ).toHaveCount(0)
    await expect
      .poll(async () => {
        const a = (await input.boundingBox())!,
          b = (await remove.boundingBox())!
        return (
          a.x >= 0 &&
          b.x + b.width <= width &&
          a.x + a.width <= b.x &&
          Math.abs(a.y - b.y) < 3
        )
      })
      .toBe(true)
    expect(
      await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth),
    ).toBe(true)
    if (process.env.BUILD_SCREENSHOT_DIR)
      await page.screenshot({
        path: `${process.env.BUILD_SCREENSHOT_DIR}/material-edit-${width}.png`,
        animations: "disabled",
      })
  })
}

test("素材分页未加载完整不能编辑保存，加载后保留全部素材", async ({ page }) => {
  const api = await buildsBoundary(page)
  let release: () => void = () => {}
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  let pages = 0
  await page.route("**/build-drafts/*/dramas/*/materials?**", async (route) => {
    pages++
    const second =
      new URL(route.request().url()).searchParams.get("cursor") === "tail"
    if (second) await held
    await route.fulfill({
      json: {
        items: [
          {
            material_id: second ? "tail-id" : "head-id",
            file_name: second ? "尾页素材.mp4" : "首页素材.mp4",
            group_no: 1,
            position: second ? 2 : 1,
            shared_with_other_drama: false,
          },
        ],
        next_cursor: second ? null : "tail",
      },
    })
  })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "查看与调整素材" }).first().click()
  await expect(
    page.getByText("正在读取完整分组，已加载 1 份素材…"),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存素材分组" }),
  ).toBeDisabled()
  await expect(page.getByLabel("组号", { exact: true })).toHaveCount(0)
  release()
  await page.getByLabel("组号", { exact: true }).first().fill("2")
  await page.getByRole("button", { name: "保存素材分组" }).click()
  await expect(page.getByRole("dialog", { name: /调整素材/ })).toHaveCount(0)
  expect(pages).toBe(2)
  expect(
    api.requests
      .find((r) => r.method === "PATCH" && r.path.endsWith("/groups"))!
      .body.groups.flat()
      .sort(),
  ).toEqual(["head-id", "tail-id"])
})

test("小程序保存后立即保留名称，后台更新不重新全量准备", async ({ page }) => {
  const api = await buildsBoundary(page)
  let selected = false,
    backgroundReads = 0
  let release: () => void = () => {}
  const held = new Promise<void>((resolve) => {
    release = resolve
  })
  await page.route(`**/build-drafts/${D}/minis**`, async (route) => {
    if (route.request().method() === "POST") {
      selected = true
      return route.fallback()
    }
    if (selected) {
      backgroundReads++
      await held
    }
    return route.fallback()
  })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "选择小程序", exact: true }).click()
  await page
    .getByRole("dialog", { name: "选择推广小程序" })
    .getByRole("button", { name: /LemonShow/ })
    .click()
  await expect(
    page.getByRole("dialog", { name: "选择推广小程序" }),
  ).toHaveCount(0)
  await expect.poll(() => backgroundReads).toBeGreaterThan(0)
  await expect(page.getByText("LemonShow", { exact: true })).toBeVisible()
  await expect(
    page.getByText("正在读取可用小程序…", { exact: true }),
  ).toHaveCount(0)
  expect(
    api.requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/prepare"),
    ),
  ).toHaveLength(0)
  release()
})

test("后台准备状态变化保留已调整素材组号且恢复后可保存", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "查看与调整素材" }).first().click()
  const input = page.getByLabel("组号", { exact: true }).first()
  await input.fill("7")
  api.summary.status = "PREPARING"
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    })
    window.dispatchEvent(new Event("visibilitychange"))
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    })
    window.dispatchEvent(new Event("visibilitychange"))
  })
  await expect(input).toBeDisabled()
  api.summary.status = "READY"
  await expect(input).toBeEnabled()
  await expect(input).toHaveValue("7")
  await page.getByRole("button", { name: "保存素材分组" }).click()
  await expect
    .poll(
      () =>
        api.requests.filter(
          (r) => r.method === "PATCH" && r.path.endsWith("/groups"),
        ).length,
    )
    .toBe(1)
  await expect(page.getByRole("dialog", { name: /调整素材/ })).toHaveCount(0)
  expect(
    api.requests.filter(
      (r) => r.method === "GET" && r.path.endsWith("/materials"),
    ),
  ).toHaveLength(1)
})
