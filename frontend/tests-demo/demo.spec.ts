import { expect, type Page, test } from "@playwright/test"

// 体验版端到端检查：隔离（不发 /api、不碰生产存储）、逐页巡检、主线引导全程跑通。
const TENANT = "d0d0d0d0-0000-4000-8000-000000000065"
const BC = "7400000000000000101"
const tenantPage = (page = "") => `/tenants/${TENANT}${page}?bc_id=${BC}`

/** 所有页面（路由路径），逐页巡检用。 */
const PAGES = [
  tenantPage(),
  tenantPage("/accounts"),
  tenantPage("/providers"),
  tenantPage("/materials"),
  tenantPage("/strategies"),
  tenantPage("/strategies/new"),
  tenantPage("/builds/new"),
  tenantPage("/build-tasks"),
  tenantPage("/ads"),
  tenantPage("/ad-management-tasks"),
  tenantPage("/automation-rules"),
  tenantPage("/notifications"),
  tenantPage("/members"),
  "/platform/tenants",
  "/admin",
  "/settings",
]

interface Watch {
  requests: string[]
  errors: string[]
}

function watch(page: Page): Watch {
  const state: Watch = { requests: [], errors: [] }
  page.on("request", (request) => state.requests.push(request.url()))
  page.on("pageerror", (error) => state.errors.push(error.message))
  return state
}

async function gaps(page: Page) {
  return page.evaluate(
    () =>
      (window as unknown as { __tkAdaDemoGaps?: string[] }).__tkAdaDemoGaps ??
      [],
  )
}

async function enter(page: Page) {
  await page.goto("/demo/#/login")
  await page.getByRole("button", { name: "自由体验" }).click()
  await expect(page).toHaveURL(/#\/tenants\//)
  await expect(page.getByText("体验版 · 演示数据")).toBeVisible()
}

async function go(page: Page, path: string) {
  await page.evaluate((hash) => {
    window.location.hash = hash
  }, `#${path}`)
  await page.waitForTimeout(1500)
}

function expectIsolated(state: Watch, origin: string) {
  const outside = state.requests.filter(
    (url) => !url.startsWith("data:") && !url.startsWith(`${origin}/demo/`),
  )
  // 品牌图标与生产共用 /assets/brand（同源静态文件），其余一律不允许。
  expect(
    outside.filter((url) => !url.startsWith(`${origin}/assets/brand/`)),
  ).toEqual([])
  expect(state.requests.filter((url) => url.includes("/api/"))).toEqual([])
}

test("体验版不发真实请求、不读写生产存储", async ({ page, baseURL }) => {
  await page.addInitScript(() => {
    // 模拟同一浏览器里已登录生产版：体验版不得读到、也不得改动这些键。
    if (!window.location.pathname.startsWith("/demo/")) return
    window.localStorage.setItem("access_token", "PRODUCTION-TOKEN")
    ;(window as unknown as { __realStorage: Storage }).__realStorage =
      window.localStorage
  })
  const state = watch(page)
  await page.goto("/demo/#/login")
  // 生产凭证存在时体验版仍停在登录页，说明没有读到它。
  await expect(page.getByRole("button", { name: "自由体验" })).toBeVisible()
  await page.getByRole("button", { name: "自由体验" }).click()
  await expect(page).toHaveURL(/#\/tenants\//)
  for (const path of [
    tenantPage("/ads"),
    tenantPage("/builds/new"),
    "/settings",
  ])
    await go(page, path)
  const real = await page.evaluate(() => {
    const storage = (window as unknown as { __realStorage: Storage })
      .__realStorage
    return Object.fromEntries(
      Array.from({ length: storage.length }, (_, i) => {
        const key = storage.key(i) as string
        return [key, storage.getItem(key)]
      }),
    )
  })
  expect(real).toEqual({ access_token: "PRODUCTION-TOKEN" })
  expectIsolated(state, new URL(baseURL as string).origin)
  expect(state.errors).toEqual([])
  expect(await gaps(page)).toEqual([])
})

test("每个页面都有内容且没有未覆盖操作", async ({ page, baseURL }) => {
  const state = watch(page)
  await enter(page)
  for (const path of PAGES) {
    await go(page, path)
    await expect(page.locator("main")).not.toBeEmpty()
    expect(await gaps(page), path).toEqual([])
    expect(state.errors, path).toEqual([])
    await expect(page.getByText("体验版暂未覆盖这个操作")).toHaveCount(0)
  }
  expectIsolated(state, new URL(baseURL as string).origin)
})

test("主线引导可以从头走到尾", async ({ page, baseURL }) => {
  test.setTimeout(420_000)
  const state = watch(page)
  await page.goto("/demo/#/login")
  await page.getByRole("button", { name: "一键进入并开始引导" }).click()
  const card = page.locator("[data-demo-tour-card]")
  await expect(card).toBeVisible()
  const counter = card.locator("span").first()
  const total = Number(/\/(\d+)/.exec(await counter.innerText())?.[1])
  expect(total).toBeGreaterThan(30)
  for (let step = 1; step <= total; step++) {
    await expect(counter).toContainText(`${step}/${total}`, { timeout: 20_000 })
    // 等页面就绪（上传完成、预览生成等）与高亮元素定位完成。
    await expect(card.getByRole("button", { name: "处理中…" })).toHaveCount(0, {
      timeout: 25_000,
    })
    await expect(card.getByText("正在定位页面元素…")).toHaveCount(0, {
      timeout: 15_000,
    })
    const title = await card.locator("h2").innerText()
    const action = card.getByRole("button", {
      name: /使用示例视频|一键选择|填入示例/,
    })
    if (await action.count()) {
      await action.click()
      await page.waitForTimeout(2500)
    }
    const target = page.locator('[data-demo-tour-target="click"]')
    if (await target.count()) {
      // 需要点击页面元素才能继续的步骤必须能定位到元素，并且点击后自动前进。
      await target.first().click()
      if (step < total)
        await expect(counter, title).toContainText(`${step + 1}/${total}`, {
          timeout: 15_000,
        })
    } else {
      await card
        .getByRole("button", { name: step === total ? "完成" : "下一步" })
        .click()
    }
  }
  await expect(card).toHaveCount(0)
  // 走完主线后，新搭建的广告已出现在报表里。
  await go(page, tenantPage("/ads"))
  await page.getByRole("tab", { name: "系列", exact: true }).click()
  await expect(page.locator("main tbody")).toContainText("Reborn as the Alpha")
  expect(await gaps(page)).toEqual([])
  expect(state.errors).toEqual([])
  expectIsolated(state, new URL(baseURL as string).origin)
})
