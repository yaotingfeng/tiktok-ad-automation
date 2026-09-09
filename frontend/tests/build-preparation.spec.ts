import { expect, test } from "@playwright/test"
import {
  BC as bc,
  buildsBoundary,
  D,
  pickInputs,
  T as tenant,
} from "./utils/buildsBoundary"

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
test("分组先分页只读，显式编辑移除尾组保存其余完整素材", async ({ page }) => {
  const api = await buildsBoundary(page)
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("button", { name: "查看与调整素材" }).first().click()
  await expect(
    page.getByText("此素材也匹配了其他剧目", { exact: false }).first(),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "保存素材分组" }),
  ).toBeDisabled()
  await page.getByRole("button", { name: "开始调整完整分组" }).click()
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
  await page.getByRole("button", { name: "开始调整完整分组" }).click()
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
  await page.getByRole("button", { name: "开始调整完整分组" }).click()
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

test("准备输入结果按50/100服务端分页", async ({ page }) => {
  const api = await buildsBoundary(page, { inputCount: 151 })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("tab", { name: "剧目输入", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "返回修正", exact: true }),
  ).toHaveCount(50)
  expect(api.requests.filter((r) => r.path.endsWith("/inputs"))).toHaveLength(1)
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect
    .poll(() => api.requests.filter((r) => r.path.endsWith("/inputs")).length)
    .toBe(2)
  expect(
    api.requests
      .filter((r) => r.path.endsWith("/inputs"))[1]
      .query.get("cursor"),
  ).toBe("50")
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "返回修正", exact: true }),
  ).toHaveCount(100)
})

test("版权方候选只提交对应输入ID，并恢复本地准备同步结果", async ({ page }) => {
  const api = await buildsBoundary(page, { candidate: "drama" })
  await page.goto(`/tenants/${tenant}/build-drafts/${D}?bc_id=${bc}`)
  await page.getByRole("tab", { name: "剧目输入", exact: true }).click()
  await page.getByRole("button", { name: "选择对应剧目", exact: true }).click()
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
  await page.getByRole("tab", { name: "剧目输入", exact: true }).click()
  await page.getByRole("button", { name: "选择对应剧目", exact: true }).click()
  await page.getByRole("button").filter({ hasText: "候选正式剧名" }).click()
  await expect(
    page.getByText(
      "候选选择结果尚待核实，不会重发或改选。请查看原取链任务中的这条输入。",
      { exact: true },
    ),
  ).toBeVisible()
  await expect(
    page.getByRole("button").filter({ hasText: "候选正式剧名" }),
  ).toBeDisabled()
  await page.keyboard.press("Escape")
  await page.getByRole("button", { name: "选择对应剧目", exact: true }).click()
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
