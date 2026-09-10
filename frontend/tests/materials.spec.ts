import { expect, type Page, test } from "@playwright/test"
import type { MaterialPublic, UploadBatchResult } from "../src/client"
import { expectWorkspaceLayout } from "./utils/workspaceLayout"

const A = "11111111-1111-4111-8111-111111111111",
  B = "22222222-2222-4222-8222-222222222222",
  BC = "9876543210987654321",
  BC2 = "9876543210987654322",
  M = "33333333-3333-4333-8333-333333333333",
  U = "55555555-5555-4555-8555-555555555555"

const hint =
  "请在素材文件名中包含完整剧目名称。广告搭建时，系统会根据剧名自动匹配素材。"

const url = `/tenants/${A}/materials?bc_id=${BC}`

const original: MaterialPublic = {
  material_id: M,
  bc_id: BC,
  file_name: "完整剧名_01.mp4",
  byte_size: 10,
  mime_type: "video/mp4",
  created_at: "2026-09-09T08:00:00Z",
  original_available: true,
  status: "available",
  available_account_count: 2,
  latest_advertiser_id: "7777777777777777777",
}

async function boundary(
  page: Page,
  options: {
    role?: string
    empty?: boolean
    count?: number
    deny?: number
  } = {},
) {
  const requests: {
    path: string
    method: string
    query: URLSearchParams
    body: any
  }[] = []
  const batches = new Map<string, UploadBatchResult>()
  const records = options.empty
    ? []
    : Array.from({ length: options.count || 1 }, (_, i) => ({
        ...original,
        material_id: i
          ? `66666666-6666-4666-8666-${String(i).padStart(12, "0")}`
          : M,
        file_name: i ? `完整剧名_${i + 1}.mp4` : original.file_name,
      }))
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("materials-fixture-initialized")) {
      localStorage.setItem("access_token", "materials-test-token")
      sessionStorage.setItem("materials-fixture-initialized", "true")
    }
  })
  await page.route("**/api/**", async (route) => {
    const req = route.request(),
      u = new URL(req.url()),
      path = u.pathname,
      query = u.searchParams,
      method = req.method()
    expect(req.headers().authorization).toBe("Bearer materials-test-token")
    const body = req.postData() ? req.postDataJSON() : undefined
    requests.push({ path, query, method, body })
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ json, status })
    const paged = (items: unknown[]) => {
      const limit = Number(query.get("limit") || 50),
        offset = Number(query.get("cursor") || 0)
      return {
        items: items.slice(offset, offset + limit),
        next_cursor:
          offset + limit < items.length ? String(offset + limit) : null,
      }
    }
    if (path === "/api/users/me")
      return reply({
        id: U,
        username: "operator",
        full_name: "素材测试用户",
        is_active: true,
        is_superuser: false,
      })
    if (path === "/api/me/tenants")
      return reply({
        items: [
          {
            id: A,
            name: "素材租户甲",
            active: true,
            role: options.role || "operator",
          },
          {
            id: B,
            name: "素材租户乙",
            active: true,
            role: options.role || "operator",
          },
        ].filter(
          (x) =>
            !query.get("search") ||
            x.id === query.get("search") ||
            x.name.includes(query.get("search")!),
        ),
        next_cursor: null,
      })
    if (path.endsWith("/bcs"))
      return reply({
        items: [
          { bc_id: BC, name: "素材 BC 甲", status: "ACTIVE" },
          { bc_id: BC2, name: "素材 BC 乙", status: "ACTIVE" },
        ],
        next_cursor: null,
      })
    if (options.deny)
      return reply(
        { code: "action_forbidden", message: "读取素材失败" },
        options.deny,
      )
    if (path.endsWith("/materials")) return reply(paged(records))
    if (path.endsWith("/preview"))
      return reply({
        url: `${u.origin}/original-preview?signature=secret-preview`,
        expires_in: 300,
      })
    if (path.endsWith("/ingest-sessions"))
      return reply(
        paged(
          [...batches.values()].map((b) => ({
            session_id: b.batch_id,
            bc_id: b.bc_id,
            status: "sealed",
            expected_count: b.files.length,
            total_bytes: 0,
            registration_cursor: -1,
            accepted_count: 0,
            uploaded_count: 0,
            ready_count: 0,
            failed_count: 0,
            cleaned_count: 0,
            reserved_bytes: 0,
            stored_bytes: 0,
            created_at: "2026-09-09T08:00:00Z",
          })),
        ),
      )
    if (path.includes("/upload-batches/"))
      return reply(
        batches.get(path.split("/").slice(-1)[0]!) || { code: "not_found" },
        batches.has(path.split("/").slice(-1)[0]!) ? 200 : 404,
      )
    if (path.endsWith("/attempts"))
      return reply(
        paged(
          ["7777777777777777777", "8888888888888888888"].map(
            (advertiser_id, i) => ({
              attempt_id: `attempt-${i}`,
              material_id: M,
              advertiser_id,
              connection_id: U,
              status: i ? "available" : "blocked",
              created_at: original.created_at,
            }),
          ),
        ),
      )
    if (path.endsWith("/assets"))
      return reply(
        paged([
          {
            asset_id: U,
            material_id: M,
            bc_id: BC,
            advertiser_id: original.latest_advertiser_id,
            connection_id: U,
            video_id: "VID-99999999999999999999",
            mid: "MID-88888888888888888888",
            status: "available",
          },
        ]),
      )
    if (path.endsWith(`/materials/${M}`)) return reply(original)
    return reply({ code: "fixture_missing", message: path }, 404)
  })
  await page.route("**/object-part/**", (route) =>
    route.fulfill({
      status: 200,
      headers: {
        ETag: `"part-${route.request().url().split("/").slice(-1)[0]?.split("?")[0]}"`,
      },
    }),
  )
  return { requests, batches, records }
}

test("批量上传不要求剧目或素材账户配置，选择文件不立即创建批次", async ({
  page,
}) => {
  const { requests } = await boundary(page, { empty: true })
  await page.goto(url)
  await page
    .getByRole("button", { name: "批量上传", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: "批量上传素材", exact: true })
  await expect(sheet).toBeVisible()
  await expect(sheet.getByText(hint, { exact: true })).toBeVisible()
  await expect(sheet.getByLabel("选择本地素材")).toHaveAttribute("multiple", "")
  await expect(sheet.getByLabel("所属剧目")).toHaveCount(0)
  await expect(sheet.getByRole("combobox")).toHaveCount(0)
  const chooser = page.waitForEvent("filechooser")
  await sheet.getByRole("button", { name: "选择本地素材", exact: true }).focus()
  await page.keyboard.press("Enter")
  await (await chooser).setFiles({
    name: "完整剧名.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from("0123456789"),
  })
  await expect(
    sheet.getByRole("button", { name: "开始上传 1 个文件" }),
  ).toBeEnabled()
  expect(requests.filter((r) => r.method === "POST")).toHaveLength(0)
})

test("目录显示真实账户并使用 BC 长字符串查询", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(url)
  await expect(
    page.getByText(original.file_name, { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText(original.latest_advertiser_id!, { exact: true }),
  ).toBeVisible()
  expect(
    requests.find((r) => r.path.endsWith("/materials"))?.query.get("bc_id"),
  ).toBe(BC)
})

test("viewer 可查真实来源历史和资产，不能上传", async ({ page }) => {
  const { requests } = await boundary(page, { role: "viewer" })
  await page.goto(url)
  await expect(
    page.getByRole("button", { name: "批量上传", exact: true }),
  ).toHaveCount(0)
  await page
    .getByRole("button", { name: "查看文件与账户记录", exact: true })
    .click()
  const sheet = page.getByRole("dialog", { name: "文件与账户记录" })
  await expect(
    sheet
      .getByRole("region", { name: "来源上传记录" })
      .getByText("8888888888888888888", { exact: true }),
  ).toBeVisible()
  await expect(
    sheet
      .getByRole("region", { name: "来源上传记录" })
      .getByText("7777777777777777777", { exact: true }),
  ).toBeVisible()
  await expect(
    sheet.getByText("VID-99999999999999999999", { exact: true }),
  ).toBeVisible()
  await expect(sheet.getByRole("combobox")).toHaveCount(2)
  expect(
    requests
      .filter((r) => r.path.endsWith("/assets") || r.path.endsWith("/attempts"))
      .every((r) => r.query.get("bc_id") === BC),
  ).toBe(true)
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(0)
})

test("目录分页保留 literal 搜索日期与 50/100 游标", async ({ page }) => {
  const { requests } = await boundary(page, { count: 205 })
  await page.goto(url)
  await page.getByLabel("素材文件名", { exact: true }).fill("剧%_名")
  await page.getByLabel("开始日期", { exact: true }).fill("2026-09-01")
  await page.getByLabel("结束日期", { exact: true }).fill("2026-09-09")
  await page.getByRole("button", { name: "搜索", exact: true }).click()
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(page.getByText("第 2 页", { exact: true })).toBeVisible()
  const req = requests.filter((r) => r.path.endsWith("/materials")).pop()!
  expect(req.query.get("query")).toBe("剧%_名")
  expect(req.query.get("cursor")).toBe("50")
  expect(req.query.get("created_from")).toBe(
    await page.evaluate(() => new Date("2026-09-01T00:00:00").toISOString()),
  )
  await page.getByRole("combobox", { name: "每页条数" }).click()
  await page.getByRole("option", { name: "100 条", exact: true }).click()
  await expect(
    page
      .getByRole("region", { name: "素材目录" })
      .getByText("第 1 页", { exact: true }),
  ).toBeVisible()
  expect(
    requests
      .filter((r) => r.path.endsWith("/materials"))
      .pop()!
      .query.get("limit"),
  ).toBe("100")
})

test("403 不伪装空库且保留登录", async ({ page }) => {
  await boundary(page, { deny: 403 })
  await page.goto(url)
  await expect(
    page.getByRole("alert").getByText("无权访问此页面", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("还没有上传素材", { exact: true })).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "materials-test-token",
  )
})

test("批次历史真实计数与服务端分页，按需预览且 URL 不缓存", async ({
  page,
}) => {
  const { requests, batches } = await boundary(page)
  for (let i = 0; i < 105; i++) {
    const id = `44444444-4444-4444-8444-${String(i).padStart(12, "0")}`
    batches.set(id, { batch_id: id, bc_id: BC, status: "available", files: [] })
  }
  await page.route("**/original-preview?**", () => {})
  await page.goto(url)
  expect(requests.filter((r) => r.path.endsWith("/preview"))).toHaveLength(0)
  await page
    .getByRole("button", { name: "查看文件与账户记录", exact: true })
    .click()
  await page.getByRole("button", { name: "预览原文件", exact: true }).click()
  await expect(page.locator("video")).toHaveAttribute("src", /original-preview/)
  expect(
    requests.find((r) => r.path.endsWith("/preview"))?.query.get("bc_id"),
  ).toBe(BC)
  expect(
    await page.evaluate(() => JSON.stringify(sessionStorage)),
  ).not.toContain("secret-preview")
  await page.getByRole("button", { name: "关闭", exact: true }).click()
  await page.getByRole("tab", { name: "上传队列", exact: true }).click()
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  expect(
    requests
      .filter((r) => r.path.endsWith("/ingest-sessions"))
      .pop()!
      .query.get("cursor"),
  ).toBe("100")
  expect(
    requests.filter((r) => r.path.includes("/ingest-sessions/")).length,
  ).toBe(0)
})

test("选文件后关闭 Sheet 可取消丢弃，保留输入且没有 POST", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.goto(url)
  await page.getByRole("button", { name: "批量上传", exact: true }).click()
  await page.getByLabel("选择本地素材").setInputFiles({
    name: "未开始.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from("0123"),
  })
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await page.getByRole("button", { name: "留在当前页", exact: true }).click()
  await expect(
    page
      .getByRole("dialog", { name: "批量上传素材" })
      .getByText("未开始.mp4", { exact: true }),
  ).toBeVisible()
  expect(requests.filter((r) => r.method === "POST")).toHaveLength(0)
})

for (const width of [1440, 900, 390])
  test(`素材工作区 ${width}px 表格滚动与上传面板`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    await boundary(page, { count: 105 })
    await page.goto(url)
    await expect(
      page.getByText(original.file_name, { exact: true }),
    ).toBeVisible()
    await expectWorkspaceLayout(page)
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true)
    expect(
      await page
        .locator('[data-slot="table-container"]')
        .first()
        .evaluate((el) => el.clientHeight),
    ).toBeLessThan(600)
    await page.screenshot({
      path: testInfo.outputPath(`materials-${width}.png`),
    })
    await page.getByRole("button", { name: "批量上传", exact: true }).click()
    await page.getByLabel("选择本地素材").setInputFiles([
      {
        name: "完整剧名_01.mp4",
        mimeType: "video/mp4",
        buffer: Buffer.alloc(1024),
      },
      {
        name: "完整剧名_02.mp4",
        mimeType: "video/mp4",
        buffer: Buffer.alloc(2048),
      },
    ])
    await expect(
      page.getByRole("button", { name: "开始上传 2 个文件", exact: true }),
    ).toBeInViewport()
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true)
    await expect(
      page.getByRole("dialog", { name: "批量上传素材", exact: true }),
    ).toBeInViewport({ ratio: 1 })
    await page.screenshot({
      path: testInfo.outputPath(`materials-upload-${width}.png`),
    })
  })

test("无有效 BC 不开放上传，空库与读取失败分开显示", async ({ page }) => {
  await boundary(page, { empty: true })
  await page.route("**/bcs?**", (route) =>
    route.fulfill({ json: { items: [], next_cursor: null } }),
  )
  await page.goto(`/tenants/${A}/materials`)
  await expect(
    page.getByText("请先选择有效的 BC", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "批量上传", exact: true }),
  ).toHaveCount(0)
})

test("会话 401 回登录且保留合法素材批次返回路径", async ({ page }) => {
  await boundary(page)
  await page.route("**/api/tenants/*/materials?**", (route) =>
    route.fulfill({ status: 401, json: { detail: "expired" } }),
  )
  await page.goto(url)
  await expect(page).toHaveURL(/\/login/)
  // The login route must still have an internal return URL, never an object signature.
  expect(await page.evaluate(() => JSON.stringify(sessionStorage))).toContain(
    `/tenants/${A}/materials`,
  )
})

test("大批导入一次选择 20000 文件仅渲染 100 行并保留同名文件", async ({
  page,
}) => {
  const { requests } = await boundary(page, { empty: true })
  await page.goto(url)
  await page
    .getByRole("button", { name: "批量上传", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: "批量上传素材", exact: true })
  await sheet.getByLabel("选择本地素材").evaluate((input: HTMLInputElement) => {
    const files = new DataTransfer()
    for (let index = 0; index < 20000; index++) {
      files.items.add(
        new File(
          [String(index).padStart(5, "0")],
          `完整剧名_${Math.floor(index / 2)}.mp4`,
          { type: "video/mp4", lastModified: 1000 },
        ),
      )
    }
    input.files = files.files
    input.dispatchEvent(new Event("change", { bubbles: true }))
  })
  await expect(
    sheet.getByRole("button", { name: "开始上传 20000 个文件" }),
  ).toBeEnabled()
  await expect(sheet.getByRole("listitem")).toHaveCount(100)
  await expect(
    sheet.getByText("已选择 20000 个文件", { exact: false }),
  ).toBeVisible()
  await sheet.getByRole("button", { name: "下一页待选文件" }).click()
  await expect(sheet.getByRole("listitem")).toHaveCount(100)
  await expect(sheet.getByText("完整剧名_50.mp4", { exact: true })).toHaveCount(
    2,
  )
  expect(requests.filter((request) => request.method === "POST")).toHaveLength(
    0,
  )
})

test("大批导入超过 20000 明确阻止且仍保持有界窗口", async ({ page }) => {
  await boundary(page, { empty: true })
  await page.goto(url)
  await page
    .getByRole("button", { name: "批量上传", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: "批量上传素材", exact: true })
  await sheet.getByLabel("选择本地素材").evaluate((input: HTMLInputElement) => {
    const files = new DataTransfer()
    for (let index = 0; index < 20001; index++)
      files.items.add(
        new File(["x"], `剧名_${index}.mp4`, { type: "video/mp4" }),
      )
    input.files = files.files
    input.dispatchEvent(new Event("change", { bubbles: true }))
  })
  await expect(
    sheet.getByText("一次导入最多 20000 个文件，请移除部分文件后继续。", {
      exact: true,
    }),
  ).toBeVisible()
  await expect(
    sheet.getByRole("button", { name: "开始上传 20001 个文件" }),
  ).toBeDisabled()
  await expect(sheet.getByRole("listitem")).toHaveCount(100)
})

test("暂存原件清理后按需预览账户素材，不持久化远端 URL 或改写可用状态", async ({
  page,
}) => {
  const { requests } = await boundary(page, { role: "viewer" })
  await page.route(`**/materials/${M}?**`, (route) =>
    route.fulfill({ json: { ...original, original_available: false } }),
  )
  let reads = 0
  await page.route("**/remote-preview?**", (route) => {
    expect(route.request().method()).toBe("GET")
    expect(new URL(route.request().url()).searchParams.get("bc_id")).toBe(BC)
    reads++
    return route.fulfill({
      headers: { "Cache-Control": "no-store" },
      json: {
        url: "https://video.test/preview?signature=remote-preview-secret",
        advertiser_id: "7777777777777777777",
        video_id: "actual-target-video",
        width: 720,
        height: 1280,
        duration: 30,
        format: "mp4",
      },
    })
  })
  await page.route("https://video.test/**", () => {})
  await page.goto(url)
  await page
    .getByRole("button", { name: "查看文件与账户记录", exact: true })
    .click()
  await expect(
    page.getByText("暂无可读取的暂存原件", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "预览原文件", exact: true }),
  ).toHaveCount(0)
  expect(reads).toBe(0)
  await page.getByRole("button", { name: "预览账户素材", exact: true }).click()
  await expect(page.getByLabel("账户素材视频预览")).toHaveAttribute(
    "src",
    /remote-preview-secret/,
  )
  await expect(
    page.getByText("actual-target-video", { exact: true }),
  ).toBeVisible()
  expect(
    await page.evaluate(() =>
      JSON.stringify({ local: localStorage, session: sessionStorage }),
    ),
  ).not.toContain("remote-preview-secret")
  await page.getByRole("button", { name: "关闭", exact: true }).click()
  await page
    .getByRole("button", { name: "查看文件与账户记录", exact: true })
    .click()
  await expect(page.locator("video")).toHaveCount(0)
  expect(reads).toBe(1)
  expect(requests.filter((request) => request.method !== "GET")).toHaveLength(0)
})

test("远端预览读取失败只显示暂无法预览，关闭后迟到回复不进入新范围", async ({
  page,
}) => {
  await boundary(page, { role: "viewer" })
  await page.route("**/remote-preview?**", (route) =>
    route.fulfill({
      status: 409,
      json: { code: "material_preview_unavailable", message: "暂无法预览" },
    }),
  )
  await page.goto(url)
  await page
    .getByRole("button", { name: "查看文件与账户记录", exact: true })
    .click()
  await page.getByRole("button", { name: "预览账户素材", exact: true }).click()
  await expect(
    page.getByText("暂无法预览，请稍后重新获取。", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("dialog").getByText("账户素材可用", { exact: true }).first(),
  ).toBeVisible()
  let release!: () => void
  const pending = new Promise<void>((resolve) => {
    release = resolve
  })
  let called = false
  await page.route("**/remote-preview?**", async (route) => {
    called = true
    await pending
    await route
      .fulfill({
        json: {
          url: "https://video.test/late?secret=late-preview",
          advertiser_id: "7777777777777777777",
          video_id: "late-video",
          width: 720,
          height: 1280,
          duration: 30,
          format: "mp4",
        },
      })
      .catch(() => {})
  })
  await page.getByRole("button", { name: "预览账户素材", exact: true }).click()
  await expect.poll(() => called).toBe(true)
  await page.getByRole("button", { name: "关闭", exact: true }).click()
  release()
  await page
    .getByRole("button", { name: "查看文件与账户记录", exact: true })
    .click()
  await expect(page.locator("video")).toHaveCount(0)
  await expect(page.getByText("late-video", { exact: true })).toHaveCount(0)
})

test("账户预览随当前登录失效立即清除，不能继续请求预览", async ({ page }) => {
  await boundary(page, { role: "viewer" })
  let reads = 0
  await page.route("**/remote-preview?**", (route) => {
    reads++
    return route.fulfill({
      json: {
        url: "https://video.test/preview?signature=auth-bound",
        advertiser_id: "7777777777777777777",
        video_id: "auth-bound-video",
        width: 720,
        height: 1280,
        duration: 30,
        format: "mp4",
      },
    })
  })
  await page.route("https://video.test/**", () => {})
  await page.goto(url)
  await page
    .getByRole("button", { name: "查看文件与账户记录", exact: true })
    .click()
  await page.getByRole("button", { name: "预览账户素材", exact: true }).click()
  await expect(page.locator("video")).toHaveCount(1)
  await page.evaluate(() => {
    localStorage.setItem("access_token", "different-session-token")
    window.dispatchEvent(
      new StorageEvent("storage", {
        key: "access_token",
        newValue: "different-session-token",
      }),
    )
  })
  await expect(page.locator("video")).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: "预览账户素材", exact: true }),
  ).toBeDisabled()
  expect(reads).toBe(1)
})
