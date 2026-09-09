import { expect, type Page, test } from "@playwright/test"
import type { MaterialPublic, UploadBatchResult } from "../src/client"

const A = "11111111-1111-4111-8111-111111111111",
  B = "22222222-2222-4222-8222-222222222222",
  BC = "9876543210987654321",
  BC2 = "9876543210987654322",
  M = "33333333-3333-4333-8333-333333333333",
  BATCH = "44444444-4444-4444-8444-444444444444",
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
    unknownCreate?: boolean
    unknownRead?: boolean
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
    if (path.endsWith("/upload-batches") && method === "POST") {
      const batch: UploadBatchResult = {
        batch_id: BATCH,
        bc_id: body.bc_id,
        status: "receiving",
        files: body.files.map((f: any, i: number) => ({
          material_id: i
            ? `33333333-3333-4333-8333-${String(i).padStart(12, "0")}`
            : M,
          upload_id: U,
          file_name: f.file_name,
          byte_size: f.size,
          part_size: 4,
          part_count: Math.ceil(f.size / 4),
          status: "receiving",
          received_bytes: null,
          can_retry: false,
        })),
      }
      batches.set(BATCH, batch)
      if (options.unknownCreate) return route.abort("failed")
      return reply(batch, 201)
    }
    if (path.includes("/upload-requests/"))
      return options.unknownRead
        ? reply({ code: "upload_batch_not_found" }, 404)
        : reply(batches.get(BATCH))
    if (path.endsWith("/preview"))
      return reply({
        url: `${u.origin}/original-preview?signature=secret-preview`,
        expires_in: 300,
      })
    if (path.endsWith("/upload-batches"))
      return reply(
        paged(
          [...batches.values()].map((b) => ({
            batch_id: b.batch_id,
            bc_id: b.bc_id,
            status: b.status,
            file_count: b.files.length,
            created_at: "2026-09-09T08:00:00Z",
          })),
        ),
      )
    if (path.includes("/upload-batches/"))
      return reply(
        batches.get(path.split("/").slice(-1)[0]!) || { code: "not_found" },
        batches.has(path.split("/").slice(-1)[0]!) ? 200 : 404,
      )
    if (path.endsWith("/sign"))
      return reply({
        url: `${u.origin}/object-part/${path.split("/").slice(-4)[0]}/${path.split("/").slice(-2)[0]}?signature=secret`,
        expires_in: 900,
      })
    if (path.endsWith("/complete")) {
      for (const b of batches.values()) {
        const f = b.files.find(
          (f) => f.material_id === path.split("/").slice(-2)[0],
        )
        if (f) {
          f.status = "stored"
          f.received_bytes = f.byte_size
          b.status = "stored"
        }
      }
      return reply({ task_id: U })
    }
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
async function start(page: Page, names = ["完整剧名.mp4"]) {
  await page
    .getByRole("button", { name: "批量上传", exact: true })
    .first()
    .click()
  await page.getByLabel("选择本地素材").setInputFiles(
    names.map((name) => ({
      name,
      mimeType: "video/mp4",
      buffer: Buffer.from("0123456789"),
    })),
  )
  await page
    .getByRole("button", { name: `开始上传 ${names.length} 个文件` })
    .click()
}
test("分片只读取 Blob 切片，真实字节与 ETag 完成，切页签不重建批次", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  const parts: Buffer[] = []
  await page.addInitScript(() => {
    File.prototype.arrayBuffer = () =>
      Promise.reject(new Error("full_file_read_forbidden"))
  })
  await page.route("**/object-part/**", (route) => {
    expect(route.request().headers().authorization).toBeUndefined()
    parts.push(route.request().postDataBuffer()!)
    return route.fulfill({
      status: 200,
      headers: { ETag: `"etag-${parts.length}"` },
    })
  })
  await page.goto(url)
  await start(page)
  await expect(page).toHaveURL(/tab=uploads/)
  await expect(
    page
      .getByRole("table")
      .getByText("原文件已接收，等待平台上传", { exact: true }),
  ).toBeVisible()
  expect(parts.map((p) => p.toString())).toEqual(["0123", "4567", "89"])
  expect(requests.find((r) => r.path.endsWith("/complete"))?.body).toEqual({
    parts: [
      { part_number: 1, etag: '"etag-1"' },
      { part_number: 2, etag: '"etag-2"' },
      { part_number: 3, etag: '"etag-3"' },
    ],
  })
  await page.getByRole("tab", { name: "素材库", exact: true }).click()
  await page.getByRole("tab", { name: "上传队列", exact: true }).click()
  expect(
    requests.filter(
      (r) => r.path.endsWith("/upload-batches") && r.method === "POST",
    ),
  ).toHaveLength(1)
  await expect(page.getByText("10 B / 10 B", { exact: true })).toBeVisible()
})
test("分片失败最多三次，不确认无 ETag 字节", async ({ page }) => {
  const { requests } = await boundary(page)
  let puts = 0
  await page.route("**/object-part/**", (route) => {
    puts++
    return route.fulfill({ status: 503 })
  })
  await page.goto(url)
  await start(page)
  await expect(
    page.getByText("传输未完成，请检查网络后继续。", { exact: true }),
  ).toBeVisible()
  expect(puts).toBe(3)
  expect(requests.filter((r) => r.path.endsWith("/complete"))).toHaveLength(0)
  await expect(page.getByText("0 B / 10 B", { exact: true })).toBeVisible()
})
test("complete 响应丢失先读批次，不重复传片与创建批次", async ({ page }) => {
  const { requests, batches } = await boundary(page)
  let complete = 0,
    puts = 0
  await page.route("**/object-part/**", (route) => {
    puts++
    return route.fulfill({ status: 200, headers: { ETag: '"ok"' } })
  })
  await page.route("**/materials/*/complete", async (route) => {
    complete++
    const b = batches.get(BATCH)!
    b.files[0].status = "stored"
    b.files[0].received_bytes = 10
    await route.abort("failed")
  })
  await page.goto(url)
  await start(page)
  await expect(
    page
      .getByRole("table")
      .getByText("原文件已接收，等待平台上传", { exact: true }),
  ).toBeVisible()
  await page.getByRole("button", { name: "刷新状态", exact: true }).click()
  expect(complete).toBe(1)
  expect(puts).toBe(3)
  expect(
    requests.filter(
      (r) => r.method === "POST" && r.path.endsWith("/upload-batches"),
    ),
  ).toHaveLength(1)
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
test("刷新不能自动恢复 File，重新选错文件拒绝，正确原文件仅续传缺失片", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  let fail = true
  const sent: string[] = []
  await page.route("**/object-part/**", (route) => {
    const content = route.request().postDataBuffer()!.toString()
    sent.push(content)
    return route.fulfill({
      status: content === "4567" && fail ? 503 : 200,
      headers: { ETag: `"${content}"` },
    })
  })
  await page.goto(url)
  await start(page)
  await expect(
    page.getByText("传输未完成，请检查网络后继续。", { exact: true }),
  ).toBeVisible()
  expect(sent).toEqual(["0123", "4567", "4567", "4567"])
  page.on("dialog", (dialog) => dialog.accept())
  await page.reload()
  await expect(page.getByLabel("重新选择原文件")).toBeVisible()
  expect(sent).toHaveLength(4)
  await page.getByLabel("重新选择原文件").setInputFiles({
    name: "另一个文件.mp4",
    mimeType: "video/mp4",
    buffer: Buffer.from("0123456789"),
  })
  await expect(
    page.getByText("所选文件与原文件不一致，请重新选择原文件。", {
      exact: true,
    }),
  ).toBeVisible()
  expect(sent).toHaveLength(4)
  await page.evaluate(
    ({ key, id }) => {
      const meta = JSON.parse(sessionStorage.getItem(key)!).records[id]
          .identity,
        input = document.querySelector<HTMLInputElement>(`#resume-${id}`)!
      const dt = new DataTransfer()
      dt.items.add(
        new File(["XXXX456789"], meta.name, {
          type: meta.type,
          lastModified: meta.lastModified,
        }),
      )
      input.files = dt.files
      input.dispatchEvent(new Event("change", { bubbles: true }))
    },
    { key: `materials-transfer:${A}:${BC}`, id: M },
  )
  await expect(
    page.getByText("所选文件与原文件不一致，请重新选择原文件。", {
      exact: true,
    }),
  ).toBeVisible()
  expect(sent).toHaveLength(4)
  fail = false
  await page.evaluate(
    ({ key, id }) => {
      const meta = JSON.parse(sessionStorage.getItem(key)!).records[id]
          .identity,
        input = document.querySelector<HTMLInputElement>(`#resume-${id}`)!
      const dt = new DataTransfer()
      dt.items.add(
        new File(["0123456789"], meta.name, {
          type: meta.type,
          lastModified: meta.lastModified,
        }),
      )
      input.files = dt.files
      input.dispatchEvent(new Event("change", { bubbles: true }))
    },
    { key: `materials-transfer:${A}:${BC}`, id: M },
  )
  await expect(
    page
      .getByRole("table")
      .getByText("原文件已接收，等待平台上传", { exact: true }),
  ).toBeVisible()
  expect(sent).toEqual(["0123", "4567", "4567", "4567", "4567", "89"])
  expect(
    requests.filter(
      (r) => r.path.endsWith("/upload-batches") && r.method === "POST",
    ),
  ).toHaveLength(1)
})
for (const target of ["tenant", "bc"]) {
  test(`传输中切换 ${target} 先守卫，留在当前页保留传输，确认离开取消原范围请求`, async ({
    page,
  }) => {
    const { requests } = await boundary(page)
    let held: any
    await page.route("**/object-part/**", (route) => {
      held = route
    })
    await page.goto(url)
    await start(page)
    await expect(page).toHaveURL(/tab=uploads/)
    await page.getByRole("tab", { name: "素材库", exact: true }).click()
    expect(held).toBeTruthy()
    const switchContext = async () => {
      await page
        .getByRole("combobox", {
          name: target === "tenant" ? "当前租户" : "当前 BC",
          exact: true,
        })
        .click()
      await page
        .getByRole("option", {
          name: target === "tenant" ? /素材租户乙/ : /素材 BC 乙/,
        })
        .click()
    }
    await switchContext()
    await expect(
      page.getByRole("dialog", { name: "暂停本地传输并离开？" }),
    ).toBeVisible()
    await page.getByRole("button", { name: "留在当前页", exact: true }).click()
    expect(page.url()).toContain(A)
    expect(page.url()).toContain(BC)
    await switchContext()
    await page.getByRole("button", { name: "暂停并离开", exact: true }).click()
    await expect(page).toHaveURL(
      target === "tenant" ? new RegExp(B) : new RegExp(BC2),
    )
    await held
      .fulfill({ status: 200, headers: { ETag: '"late"' } })
      .catch(() => {})
    expect(requests.filter((r) => r.path.endsWith("/complete"))).toHaveLength(0)
    expect(
      requests
        .filter((r) => r.method === "POST")
        .every((r) => r.path.includes(A)),
    ).toBe(true)
  })
}
test("缺 ETag 不推进百分比或 complete；签名 URL 不持久化", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.route("**/object-part/**", (route) =>
    route.fulfill({ status: 200 }),
  )
  await page.goto(url)
  await start(page)
  await expect(
    page.getByText(
      "存储未返回可读取的 ETag，分片未确认；请检查存储 CORS 配置。",
      { exact: true },
    ),
  ).toBeVisible()
  expect(requests.filter((r) => r.path.endsWith("/complete"))).toHaveLength(0)
  const stored = await page.evaluate(() => JSON.stringify(sessionStorage))
  expect(stored).not.toContain("signature=")
  expect(stored).not.toContain("object-part")
})
test("批量明确失败重试排除未知与已完成；viewer 即使 can_retry 也无动作", async ({
  page,
}) => {
  const { batches, requests } = await boundary(page)
  batches.set(BATCH, {
    batch_id: BATCH,
    bc_id: BC,
    status: "blocked",
    files: ["blocked", "result_unknown", "available"].map((status, i) => ({
      material_id: `33333333-3333-4333-8333-${String(i).padStart(12, "0")}`,
      upload_id: U,
      file_name: `队列文件${i}.mp4`,
      byte_size: 10,
      part_size: 4,
      part_count: 3,
      status: status as any,
      received_bytes: 10,
      can_retry: true,
    })),
  })
  const retried: string[] = []
  await page.route("**/materials/*/retry", (route) => {
    retried.push(
      new URL(route.request().url()).pathname.split("/").slice(-2)[0],
    )
    return route.fulfill({
      json: {
        ...batches.get(BATCH)!.files[0],
        status: "stored",
        can_retry: false,
      },
    })
  })
  await page.goto(`${url}&tab=uploads&batch_id=${BATCH}`)
  await page
    .getByRole("button", { name: "重试明确失败项", exact: true })
    .click()
  expect(retried).toEqual(["33333333-3333-4333-8333-000000000000"])
  await expect(
    page.getByRole("button", { name: "查看核实进度", exact: true }),
  ).toBeVisible()
  expect(
    requests.filter(
      (r) => r.path.endsWith("/upload-batches") && r.method === "POST",
    ),
  ).toHaveLength(0)
})
test("批次 POST 丢失按原 request_id 找回，不重复创建并保留本地 File 继续上传", async ({
  page,
}) => {
  const { requests } = await boundary(page, { unknownCreate: true })
  await page.goto(url)
  await start(page)
  await expect(page).toHaveURL(new RegExp(BATCH))
  await expect(
    page
      .getByRole("table")
      .getByText("原文件已接收，等待平台上传", { exact: true }),
  ).toBeVisible()
  const creates = requests.filter(
    (r) => r.method === "POST" && r.path.endsWith("/upload-batches"),
  )
  expect(creates).toHaveLength(1)
  expect(
    requests.find((r) => r.path.includes("/upload-requests/"))?.path,
  ).toContain(creates[0].body.request_id)
})
test("未知批次回查 404 后刷新仍用原请求，不能再创建", async ({ page }) => {
  const { requests } = await boundary(page, {
    unknownCreate: true,
    unknownRead: true,
  })
  await page.goto(url)
  await start(page)
  await expect(
    page.getByText(
      "批次创建结果尚未确认。关闭面板后可在上传队列按原请求核实；不会重复创建批次。",
      { exact: true },
    ),
  ).toBeVisible()
  await page.getByRole("button", { name: "取消", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "核实批次创建结果", exact: true }),
  ).toBeVisible()
  page.on("dialog", (dialog) => dialog.accept())
  await page.reload()
  await page
    .getByRole("button", { name: "核实批次创建结果", exact: true })
    .click()
  await expect(
    page.getByRole("button", { name: "批量上传", exact: true }),
  ).toBeDisabled()
  const creates = requests.filter(
    (r) => r.method === "POST" && r.path.endsWith("/upload-batches"),
  )
  expect(creates).toHaveLength(1)
  expect(
    requests
      .filter((r) => r.path.includes("/upload-requests/"))
      .every((r) => r.path.endsWith(creates[0].body.request_id)),
  ).toBe(true)
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
      .filter((r) => r.path.endsWith("/upload-batches"))
      .pop()!
      .query.get("cursor"),
  ).toBe("50")
  expect(
    requests.filter((r) => r.path.includes("/upload-batches/")).length,
  ).toBe(0)
})
test("两路文件并发有界，完成一个才传第三个", async ({ page }) => {
  await boundary(page)
  let concurrent = 0,
    max = 0,
    started = 0
  const held: any[] = []
  await page.route("**/object-part/**", (route) => {
    concurrent++
    started++
    max = Math.max(max, concurrent)
    held.push(route)
  })
  await page.goto(url)
  await start(page, ["完整剧名1.mp4", "完整剧名2.mp4", "完整剧名3.mp4"])
  await expect.poll(() => started).toBe(2)
  expect(max).toBe(2)
  for (let i = 0; i < 9; i++) {
    await expect.poll(() => held.length).toBeGreaterThan(0)
    const r = held.shift()
    concurrent--
    await r.fulfill({ status: 200, headers: { ETag: `"${i}"` } })
  }
  await expect(
    page
      .getByRole("table")
      .getByText("原文件已接收，等待平台上传", { exact: true }),
  ).toHaveCount(3)
  expect(max).toBeLessThanOrEqual(2)
})
test("viewer 队列不因服务端 can_retry=true 展示写按钮", async ({ page }) => {
  const { batches } = await boundary(page, { role: "viewer" })
  batches.set(BATCH, {
    batch_id: BATCH,
    bc_id: BC,
    status: "blocked",
    files: [
      {
        material_id: M,
        upload_id: U,
        file_name: "失败文件.mp4",
        byte_size: 10,
        part_size: 4,
        part_count: 3,
        status: "blocked",
        received_bytes: 10,
        can_retry: true,
      },
    ],
  })
  await page.goto(`${url}&tab=uploads&batch_id=${BATCH}`)
  await expect(page.getByText("失败文件.mp4", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "重试", exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: "重试明确失败项", exact: true }),
  ).toHaveCount(0)
})
test("complete 请求未抵达时先读取进度，再用原分片清单完成原对象", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  let completes = 0
  const bodies: any[] = []
  await page.route("**/materials/*/complete", async (route) => {
    completes++
    bodies.push(route.request().postDataJSON())
    if (completes === 1) return route.abort("failed")
    return route.fallback()
  })
  await page.goto(url)
  await start(page)
  await expect(
    page.getByRole("button", { name: "查看核实进度", exact: true }),
  ).toBeVisible()
  const readsBefore = requests.filter((r) =>
    r.path.includes("/upload-batches/"),
  ).length
  await page.getByRole("button", { name: "查看核实进度", exact: true }).click()
  await expect(
    page
      .getByRole("table")
      .getByText("原文件已接收，等待平台上传", { exact: true }),
  ).toBeVisible()
  expect(completes).toBe(2)
  expect(bodies[1]).toEqual(bodies[0])
  expect(
    requests.filter((r) => r.path.includes("/upload-batches/")).length,
  ).toBeGreaterThan(readsBefore)
  expect(requests.filter((r) => r.path.endsWith("/sign"))).toHaveLength(3)
})
test("result_unknown 即使 can_retry 也只读取进度，保留原文件说明", async ({
  page,
}) => {
  const { requests, batches } = await boundary(page)
  batches.set(BATCH, {
    batch_id: BATCH,
    bc_id: BC,
    status: "result_unknown",
    files: [
      {
        material_id: M,
        upload_id: U,
        file_name: "待核实原文件.mp4",
        byte_size: 10,
        part_size: 4,
        part_count: 3,
        status: "result_unknown",
        received_bytes: 10,
        can_retry: true,
        error_code: "source_upload_result_unknown",
      },
    ],
  })
  await page.goto(`${url}&tab=uploads&batch_id=${BATCH}`)
  await page.getByRole("button", { name: "查看核实进度", exact: true }).click()
  await expect(
    page.getByRole("table").getByText("上传结果待核实", { exact: true }),
  ).toBeVisible()
  expect(requests.every((r) => r.method === "GET")).toBe(true)
})
test("写入 403 停止后续上传并隐藏动作，保留用户与文件", async ({ page }) => {
  await boundary(page)
  await page.route("**/materials/*/upload-parts/*/sign", (route) =>
    route.fulfill({ status: 403, json: { code: "action_forbidden" } }),
  )
  await page.goto(url)
  await start(page)
  await expect(
    page.getByRole("table").getByText("完整剧名.mp4", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "批量上传", exact: true }),
  ).toHaveCount(0)
  await expect(page.getByLabel("重新选择原文件")).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "materials-test-token",
  )
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
test("存储恢复标识失败时保留选文件且不发送创建请求", async ({ page }) => {
  const { requests } = await boundary(page)
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem
    Storage.prototype.setItem = function (key, value) {
      if (key.endsWith(":pending"))
        throw new DOMException("quota", "QuotaExceededError")
      return original.call(this, key, value)
    }
  })
  await page.goto(url)
  await start(page)
  await expect(
    page.getByText(
      "浏览器无法保存上传恢复信息，请释放当前站点存储空间后重试。尚未创建批次。",
      { exact: true },
    ),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "开始上传 1 个文件" }),
  ).toBeEnabled()
  expect(requests.filter((r) => r.method === "POST")).toHaveLength(0)
})
test("已接收后轮询真实平台阶段，账户来自实际返回且不重发", async ({ page }) => {
  const { requests, batches } = await boundary(page)
  await page.goto(url)
  await start(page)
  await expect(
    page
      .getByRole("table")
      .getByText("原文件已接收，等待平台上传", { exact: true }),
  ).toBeVisible()
  const b = batches.get(BATCH)!
  b.files[0].status = "available"
  b.files[0].latest_advertiser_id = "12345678901234567890"
  b.status = "available"
  await expect(
    page.getByRole("table").getByText("账户素材可用", { exact: true }),
  ).toBeVisible({ timeout: 7000 })
  await expect(
    page.getByRole("table").getByText("12345678901234567890", { exact: true }),
  ).toBeVisible()
  expect(requests.filter((r) => r.path.endsWith("/complete"))).toHaveLength(1)
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
test("上传队列读取 403 立即隐藏上传入口且不注销", async ({ page }) => {
  await boundary(page)
  await page.route("**/api/tenants/*/materials/upload-batches/*", (route) =>
    route.fulfill({ status: 403, json: { code: "action_forbidden" } }),
  )
  await page.goto(`${url}&tab=uploads&batch_id=${BATCH}`)
  await expect(
    page.getByRole("alert").getByText("无权访问此页面", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "批量上传", exact: true }),
  ).toHaveCount(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "materials-test-token",
  )
})
test("传输期间读取权限失效同时取消直传，不能继续发送后续分片", async ({
  page,
}) => {
  const { requests } = await boundary(page)
  let held: any
  await page.route("**/object-part/**", (route) => {
    held = route
  })
  await page.goto(url)
  await start(page)
  await expect(page).toHaveURL(/tab=uploads/)
  const cancelled = page.waitForEvent("requestfailed", {
    predicate: (r) => r.url().includes("/object-part/"),
  })
  await page.route("**/api/tenants/*/materials/upload-batches/*", (route) =>
    route.fulfill({ status: 403, json: { code: "action_forbidden" } }),
  )
  await page.getByRole("button", { name: "刷新状态", exact: true }).click()
  await cancelled
  await held
    .fulfill({ status: 200, headers: { ETag: '"late"' } })
    .catch(() => {})
  expect(requests.filter((r) => r.path.endsWith("/sign"))).toHaveLength(1)
  expect(requests.filter((r) => r.path.endsWith("/complete"))).toHaveLength(0)
})

for (const permission of ["viewer", "revoked"] as const)
  test(`independent: ${permission} refresh of uncertain local completion stays read-only`, async ({
    page,
  }) => {
    const { requests, batches } = await boundary(page, {
      role: permission === "viewer" ? "viewer" : "operator",
    })
    batches.set(BATCH, {
      batch_id: BATCH,
      bc_id: BC,
      status: "receiving",
      files: [
        {
          material_id: M,
          upload_id: U,
          file_name: "unfinished.mp4",
          byte_size: 4,
          part_size: 4,
          part_count: 1,
          status: "receiving",
          received_bytes: null,
          can_retry: false,
        },
      ],
    })
    await page.addInitScript(
      ({ key, materialId, uploadId, batchId }) => {
        sessionStorage.setItem(
          key,
          JSON.stringify({
            batchIds: [batchId],
            records: {
              [materialId]: {
                identity: {
                  name: "unfinished.mp4",
                  size: 4,
                  type: "video/mp4",
                  lastModified: 1,
                },
                uploadId,
                parts: [
                  {
                    part_number: 1,
                    etag: '"original-part"',
                    hash: "original-hash",
                  },
                ],
                completionUnknown: true,
              },
            },
          }),
        )
      },
      {
        key: `materials-transfer:${A}:${BC}`,
        materialId: M,
        uploadId: U,
        batchId: BATCH,
      },
    )
    if (permission === "revoked") {
      await page.route("**/api/tenants/*/materials?**", (route) =>
        route.fulfill({ status: 403, json: { code: "action_forbidden" } }),
      )
      await page.goto(`${url}&batch_id=${BATCH}`)
      await expect(
        page.getByRole("alert").getByText("无权访问此页面", { exact: true }),
      ).toBeVisible()
      await page.getByRole("tab", { name: "上传队列", exact: true }).click()
    } else await page.goto(`${url}&tab=uploads&batch_id=${BATCH}`)
    await expect(
      page.getByRole("table").getByText("unfinished.mp4", { exact: true }),
    ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "批量上传", exact: true }),
    ).toHaveCount(0)
    await page.getByRole("button", { name: "刷新状态", exact: true }).click()
    await expect
      .poll(
        () =>
          requests.filter((r) => r.path.endsWith(`/upload-batches/${BATCH}`))
            .length,
      )
      .toBeGreaterThan(1)
    await expect(
      page.getByRole("button", { name: "刷新状态", exact: true }),
    ).toBeEnabled()
    await page.waitForTimeout(150)
    expect(requests.filter((r) => r.method !== "GET")).toEqual([])
  })

async function uncertainCompletionBoundary(page: Page, count = 1) {
  const setup = await boundary(page)
  const batch: UploadBatchResult = {
    batch_id: BATCH,
    bc_id: BC,
    status: "receiving",
    files: Array.from({ length: count }, (_, i) => ({
      material_id: i
        ? `33333333-3333-4333-8333-${String(i).padStart(12, "0")}`
        : M,
      upload_id: U,
      file_name: `unfinished-${i}.mp4`,
      byte_size: 4,
      part_size: 4,
      part_count: 1,
      status: "receiving",
      received_bytes: null,
      can_retry: false,
    })),
  }
  setup.batches.set(BATCH, batch)
  await page.addInitScript(
    ({ key, batch }) => {
      sessionStorage.setItem(
        key,
        JSON.stringify({
          batchIds: [batch.batch_id],
          records: Object.fromEntries(
            batch.files.map((row) => [
              row.material_id,
              {
                identity: {
                  name: row.file_name,
                  size: 4,
                  type: "video/mp4",
                  lastModified: 1,
                },
                uploadId: row.upload_id,
                parts: [
                  {
                    part_number: 1,
                    etag: '"original-part"',
                    hash: "original-hash",
                  },
                ],
                completionUnknown: true,
              },
            ]),
          ),
        }),
      )
    },
    { key: `materials-transfer:${A}:${BC}`, batch },
  )
  return setup
}

test("independent: first completion 403 stops the remaining completion requests", async ({
  page,
}) => {
  await uncertainCompletionBoundary(page, 2)
  const completes: string[] = []
  await page.route("**/materials/*/complete", (route) => {
    completes.push(route.request().url())
    return route.fulfill({ status: 403, json: { code: "action_forbidden" } })
  })
  await page.goto(`${url}&tab=uploads&batch_id=${BATCH}`)
  await expect(
    page.getByRole("table").getByText("unfinished-0.mp4", { exact: true }),
  ).toBeVisible()
  await page.getByRole("button", { name: "刷新状态", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "批量上传", exact: true }),
  ).toHaveCount(0)
  await page.waitForTimeout(150)
  expect(completes).toHaveLength(1)
})

test("independent: scope change during a recovery read never completes the old object", async ({
  page,
}) => {
  const { requests } = await uncertainCompletionBoundary(page)
  await page.goto(`${url}&tab=uploads&batch_id=${BATCH}`)
  await expect(
    page.getByRole("table").getByText("unfinished-0.mp4", { exact: true }),
  ).toBeVisible()
  let held: import("@playwright/test").Route | undefined
  await page.route(`**/upload-batches/${BATCH}`, (route) => {
    held = route
  })
  await page.getByRole("button", { name: "刷新状态", exact: true }).click()
  await expect.poll(() => !!held).toBe(true)
  await page.getByRole("combobox", { name: "当前 BC", exact: true }).click()
  await page.getByRole("option", { name: /素材 BC 乙/ }).click()
  await expect(page).toHaveURL(new RegExp(`bc_id=${BC2}`))
  await held!
    .fulfill({
      json: { batch_id: BATCH, bc_id: BC, status: "receiving", files: [] },
    })
    .catch(() => {})
  await page.waitForTimeout(150)
  expect(requests.filter((r) => r.path.endsWith("/complete"))).toEqual([])
})
