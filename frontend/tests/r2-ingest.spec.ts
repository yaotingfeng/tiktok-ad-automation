import { expect, test } from "@playwright/test"
import {
  ingestBoundary,
  location,
  queueLocation,
  SESSION,
  video,
} from "./fixtures/ingest"

async function begin(page: import("@playwright/test").Page, count = 1) {
  await page.goto(location)
  await page
    .getByRole("button", { name: "批量上传", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: "批量上传素材", exact: true })
  await sheet
    .getByLabel("选择本地素材")
    .setInputFiles(Array.from({ length: count }, (_, i) => video(i)))
  await sheet.getByRole("button", { name: `开始上传 ${count} 个文件` }).click()
}

test("generated ingest API drives independent receive/platform/cleanup stages with exact direct PUTs", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { completingPages: 1 })
  await begin(page, 3)
  await expect(page).toHaveURL(new RegExp(`batch_id=${SESSION}`))
  await expect.poll(() => f.summary.uploaded_count).toBe(3)
  await expect(
    page.getByText("已登记 3 · 已接收 3 · 平台可用 0 · 已清理 0 · 失败 0", {
      exact: true,
    }),
  ).toBeVisible({ timeout: 10000 })
  expect(f.puts).toHaveLength(6)
  expect(f.puts.every((p) => p.bytes === 4)).toBe(true)
  expect(f.calls.filter((c) => c.path.endsWith("/complete"))).toHaveLength(6)
  expect(f.calls.some((c) => c.path.includes("/upload-batches"))).toBe(false)
})

test("lost parent creation response resumes original durable request without another session POST", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { lostCreate: true })
  await begin(page)
  await expect.poll(() => f.summary.uploaded_count).toBe(1)
  expect(
    f.calls.filter(
      (c) => c.path.endsWith("/ingest-sessions") && c.method === "POST",
    ),
  ).toHaveLength(1)
  expect(f.calls.some((c) => c.path.includes("/ingest-requests/"))).toBe(true)
})

test("lost completion refresh reconciles received original without local File or repeated PUT", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { lostComplete: true })
  await begin(page)
  await expect.poll(() => f.summary.uploaded_count).toBe(1)
  const writes = f.puts.length
  await page.reload()
  await expect(
    page.getByRole("button", { name: "继续传输 / 核实接收", exact: true }),
  ).toBeEnabled()
  await page
    .getByRole("button", { name: "继续传输 / 核实接收", exact: true })
    .click()
  await expect
    .poll(() => f.calls.filter((c) => c.path.endsWith("/seal")).length)
    .toBeGreaterThan(0)
  expect(f.puts).toHaveLength(writes)
  expect(f.calls.filter((c) => c.path.endsWith("/complete"))).toHaveLength(1)
})

test("history details use bounded server pages and filters; viewer performs zero writes", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { count: 205, role: "viewer" })
  await page.goto(queueLocation)
  await expect(
    page.getByRole("button", { name: "查看详情", exact: true }),
  ).toHaveCount(100)
  await page.getByRole("button", { name: "下一页", exact: true }).click()
  await expect(
    page.getByText("完整剧名_100.mp4", { exact: true }),
  ).toBeVisible()
  expect(
    f.calls.some(
      (c) => c.path.endsWith("/files") && c.query.get("cursor") === "100",
    ),
  ).toBe(true)
  await page.getByRole("combobox", { name: "筛选导入状态" }).click()
  await page.getByRole("option", { name: "失败", exact: true }).click()
  await expect
    .poll(() =>
      f.calls.some(
        (c) =>
          c.path.endsWith("/files") &&
          c.query.get("status") === "failed" &&
          !c.query.get("cursor"),
      ),
    )
    .toBe(true)
  expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(0)
  await expect(
    page.getByRole("button", { name: "继续传输 / 核实接收" }),
  ).toHaveCount(0)
})

test("capacity waiting retains metadata and issues no signed URL or direct PUT", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { backpressure: true })
  await begin(page, 6)
  await expect
    .poll(() => f.calls.filter((c) => c.path.endsWith("/resume")).length)
    .toBeGreaterThan(0)
  await expect(
    page.getByText("等待暂存空间", { exact: true }).first(),
  ).toBeVisible({ timeout: 10000 })
  expect(f.puts).toHaveLength(0)
  expect(f.calls.some((c) => c.path.endsWith("/part-urls"))).toBe(false)
  f.capacityAvailable()
  await expect.poll(() => f.summary.uploaded_count, { timeout: 12000 }).toBe(6)
})

test("over 200 files register bounded chunks and capacity prevents draining browser bytes", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { backpressure: true })
  await begin(page, 201)
  await expect.poll(() => f.summary.accepted_count).toBe(201)
  const chunks = f.calls.filter(
    (c) => c.path.endsWith("/chunks") && c.method === "POST",
  )
  expect(chunks.map((c) => c.body.files.length)).toEqual([200, 1])
  expect(new Set(chunks.map((c) => c.body.request_id)).size).toBe(2)
  expect(f.puts).toHaveLength(0)
})

test("lost chunk response refresh retries its durable request key without duplicate files", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { lostChunk: true })
  await begin(page)
  await expect.poll(() => f.summary.accepted_count).toBe(1)
  await page.reload()
  await page
    .getByRole("button", { name: "继续传输 / 核实接收", exact: true })
    .click()
  await expect
    .poll(() => f.calls.filter((c) => c.path.endsWith("/chunks")).length)
    .toBe(2)
  const chunks = f.calls.filter((c) => c.path.endsWith("/chunks"))
  expect(chunks[1].body).toEqual(chunks[0].body)
  expect(f.files.size).toBe(1)
  expect(f.puts).toHaveLength(0)
})

test("signing permission loss stops transfer and retains login without more PUT or completion", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { denySign: true })
  await begin(page, 8)
  await expect(
    page.getByRole("button", { name: "批量上传", exact: true }),
  ).toHaveCount(0)
  expect(f.puts).toHaveLength(0)
  expect(f.calls.filter((c) => c.path.endsWith("/complete"))).toHaveLength(0)
  expect(await page.evaluate(() => localStorage.getItem("access_token"))).toBe(
    "ingest-token",
  )
})

for (const target of ["tenant", "bc"] as const) {
  test(`switching ${target} aborts in-flight direct uploads after the scope guard`, async ({
    page,
  }) => {
    const f = await ingestBoundary(page)
    const held: import("@playwright/test").Route[] = []
    await page.route("https://storage.test/**", (route) => {
      held.push(route)
    })
    await begin(page, 8)
    await expect.poll(() => held.length).toBe(8)
    const switchScope = async () => {
      await page
        .getByRole("combobox", {
          name: target === "tenant" ? "当前租户" : "当前 BC",
          exact: true,
        })
        .click()
      await page
        .getByRole("option", {
          name: target === "tenant" ? /租户乙/ : /素材 BC 乙/,
        })
        .click()
    }
    await switchScope()
    await expect(
      page.getByRole("dialog", { name: "暂停本地传输并离开？" }),
    ).toBeVisible()
    await page.getByRole("button", { name: "留在当前页", exact: true }).click()
    await switchScope()
    const cancelled = page.waitForEvent("requestfailed", {
      predicate: (r) => r.url().startsWith("https://storage.test/"),
    })
    await page.getByRole("button", { name: "暂停并离开", exact: true }).click()
    await cancelled
    await Promise.all(
      held.map((route) =>
        route
          .fulfill({ status: 200, headers: { ETag: "late" } })
          .catch(() => {}),
      ),
    )
    expect(f.calls.filter((c) => c.path.endsWith("/complete"))).toHaveLength(0)
    expect(held).toHaveLength(8)
  })
}

test("logout aborts direct PUTs before navigation even when the leave dialog is still open", async ({
  page,
}) => {
  const errors: string[] = []
  page.on("console", (message) => {
    if (
      message.type() === "error" &&
      /Maximum update depth|Cannot update a component/.test(message.text())
    )
      errors.push(message.text())
  })
  const f = await ingestBoundary(page)
  const held: import("@playwright/test").Route[] = []
  await page.route("https://storage.test/**", (route) => {
    held.push(route)
  })
  await begin(page, 5)
  await expect.poll(() => held.length).toBe(8)
  const cancelled = page.waitForEvent("requestfailed", {
    predicate: (r) => r.url().startsWith("https://storage.test/"),
  })
  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "退出登录", exact: true }).click()
  await cancelled
  await Promise.all(
    held.map((route) =>
      route.fulfill({ status: 200, headers: { ETag: "late" } }).catch(() => {}),
    ),
  )
  // Logout can replace the document after requests abort. Inspect storage only
  // once that navigation has committed; this must not race the old JS context.
  await page.waitForURL(/\/login(?:\?.*)?$/)
  expect(
    await page.evaluate(() => localStorage.getItem("access_token")),
  ).toBeNull()
  expect(f.calls.filter((c) => c.path.endsWith("/complete"))).toHaveLength(0)
  expect(held).toHaveLength(8)
  expect(errors).toEqual([])
})

test("IndexedDB unavailable leaves selection intact and creates no server session", async ({
  page,
}) => {
  const f = await ingestBoundary(page)
  await page.addInitScript(() => {
    indexedDB.open = () => {
      throw new DOMException("Denied", "SecurityError")
    }
  })
  await begin(page)
  await expect(
    page
      .getByRole("dialog", { name: "批量上传素材", exact: true })
      .getByText("完整剧名_0.mp4", { exact: true }),
  ).toBeVisible()
  expect(f.calls.filter((c) => c.method === "POST")).toHaveLength(0)
})

test("unknown original keeps read-only reconciliation and cannot request another generation", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { count: 1 })
  Object.assign(f.files.get(0)!, {
    upload_id: "existing",
    temporary_storage_status: "receiving",
    operation_status: "result_unknown",
    platform_status: "result_unknown",
    can_retry: true,
  })
  await page.goto(queueLocation)
  await expect(
    page.getByText("结果待核实，不会重复上传", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "重试此文件", exact: true }),
  ).toHaveCount(0)
  await page
    .getByRole("button", { name: "继续传输 / 核实接收", exact: true })
    .click()
  await expect
    .poll(() => f.calls.filter((c) => c.path.endsWith("/resume")).length)
    .toBe(1)
  expect(
    f.calls.some(
      (c) =>
        c.path.endsWith("/new-generation") || c.path.endsWith("/part-urls"),
    ),
  ).toBe(false)
  expect(f.puts).toHaveLength(0)
})

test("source availability and temporary cleanup poll independently without replaying ingestion", async ({
  page,
}) => {
  const f = await ingestBoundary(page)
  await begin(page)
  await expect.poll(() => f.summary.uploaded_count).toBe(1)
  const file = f.files.get(0)!
  Object.assign(file, {
    platform_status: "available",
    source_advertiser_id: "12345678901234567890",
    temporary_storage_status: "deleted",
  })
  f.summary.ready_count = 1
  f.summary.cleaned_count = 1
  await expect(
    page.getByRole("table").getByText("账户素材可用", { exact: true }),
  ).toBeVisible({ timeout: 10000 })
  await expect(
    page.getByRole("table").getByText("临时原件已清理", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("12345678901234567890", { exact: true }),
  ).toBeVisible()
  expect(f.calls.filter((c) => c.path.endsWith("/complete"))).toHaveLength(1)
  const stored = await page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>((resolve) => {
      const request = indexedDB.open("tiktok-material-transfers-v1")
      request.onsuccess = () => resolve(request.result)
    })
    const all: unknown[] = []
    for (const store of db.objectStoreNames)
      all.push(
        await new Promise((resolve) => {
          const request = db.transaction(store).objectStore(store).getAll()
          request.onsuccess = () => resolve(request.result)
        }),
      )
    db.close()
    return JSON.stringify(all)
  })
  expect(stored).not.toMatch(
    /ephemeral|storage\.test|ingest-token|"blob"|"url"/,
  )
})

test("interrupted local manifest setup resumes the same parent intent after file reselection", async ({
  page,
}) => {
  const f = await ingestBoundary(page)
  await page.goto(location)
  const requestId = await page.evaluate(async () => {
    const path = "/src/features/materials/upload-store.ts"
    const { UploadStore } = await import(/* @vite-ignore */ path)
    const store = await UploadStore.open()
    const key = crypto.randomUUID()
    await store.putImport({
      tenantId: "11111111-1111-4111-8111-111111111111",
      bcId: "9876543210987654321",
      sessionId: key,
      requestId: key,
      fileCount: 1,
      totalBytes: 8,
      metadataReady: false,
      serverSessionId: null,
      createdAt: Date.now(),
    })
    store.close()
    return key
  })
  await page.reload()
  await expect(
    page.getByRole("button", { name: "重选此导入全部文件", exact: true }),
  ).toBeEnabled()
  await page
    .getByLabel("重选此导入全部文件", { exact: true })
    .setInputFiles(video(0))
  await expect.poll(() => f.summary.uploaded_count).toBe(1)
  const created = f.calls.filter(
    (call) => call.path.endsWith("/ingest-sessions") && call.method === "POST",
  )
  expect(created).toHaveLength(1)
  expect(created[0].body.request_id).toBe(requestId)
})

test("only explicit safe retry starts a new generation and demands the original File again", async ({
  page,
}) => {
  const f = await ingestBoundary(page)
  await begin(page)
  await expect.poll(() => f.summary.uploaded_count).toBe(1)
  Object.assign(f.files.get(0)!, {
    platform_status: "blocked",
    temporary_storage_status: "deleted",
    can_retry: true,
  })
  await page.getByRole("button", { name: "刷新进度", exact: true }).click()
  await page.getByRole("button", { name: "重试此文件", exact: true }).click()
  await expect.poll(() => f.files.get(0)!.generation).toBe(2)
  await expect
    .poll(() => f.calls.filter((c) => c.path.endsWith("/seal")).length)
    .toBe(2)
  expect(f.puts).toHaveLength(2)
  await page.getByRole("button", { name: "刷新进度", exact: true }).click()
  await page.getByRole("button", { name: "重选此文件", exact: true }).click()
  await page
    .getByLabel("重选单个原文件", { exact: true })
    .evaluate((input: HTMLInputElement, modified) => {
      const selection = new DataTransfer()
      selection.items.add(
        new File([new Uint8Array(8).fill(1)], "完整剧名_0.mp4", {
          type: "video/mp4",
          lastModified: modified,
        }),
      )
      input.files = selection.files
      input.dispatchEvent(new Event("change", { bubbles: true }))
    }, f.files.get(0)!.last_modified_ms!)
  await expect
    .poll(() => f.files.get(0)?.temporary_storage_status)
    .toBe("stored")
  expect(
    f.calls.filter((c) => c.path.endsWith("/new-generation")),
  ).toHaveLength(1)
  expect(f.puts).toHaveLength(4)
})

test("abandoning a capacity-waiting file requests cancellation without signing or uploading", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { backpressure: true })
  await begin(page)
  await expect
    .poll(() => f.files.get(0)?.temporary_storage_status)
    .toBe("waiting_capacity")
  await page.getByRole("button", { name: "刷新进度", exact: true }).click()
  await page.getByRole("button", { name: "放弃此文件", exact: true }).click()
  await expect
    .poll(() => f.calls.filter((c) => c.path.endsWith("/cancel")).length)
    .toBe(1)
  expect(f.puts).toHaveLength(0)
  expect(
    f.calls.some(
      (c) =>
        c.path.endsWith("/part-urls") || c.path.endsWith("/new-generation"),
    ),
  ).toBe(false)
})

test("refresh without local files reads current facts and does not reserve another multipart window", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { count: 3 })
  await page.goto(queueLocation)
  await page
    .getByRole("button", { name: "继续传输 / 核实接收", exact: true })
    .click()
  await expect
    .poll(() => f.calls.filter((c) => /\/files\/[^/]+$/.test(c.path)).length)
    .toBe(3)
  expect(
    f.calls.filter(
      (c) => c.path.endsWith("/resume") || c.path.endsWith("/part-urls"),
    ),
  ).toHaveLength(0)
  expect(f.puts).toHaveLength(0)
  expect([...f.files.values()].every((file) => file.upload_id === null)).toBe(
    true,
  )
})

for (const width of [390, 900]) {
  test(`ingest queue ${width}px retains independent counts and scrolls only its table`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    await ingestBoundary(page, { count: 3, role: "viewer" })
    await page.goto(queueLocation)
    await expect(
      page.getByRole("table").getByText("完整剧名_0.mp4", { exact: true }),
    ).toBeVisible()
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true)
    await page.screenshot({
      path: testInfo.outputPath(`ingest-queue-${width}.png`),
    })
  })
}

test("legacy batch deep link reads historical status and source after only an explicit session 404", async ({
  page,
}) => {
  const f = await ingestBoundary(page)
  let legacyReads = 0
  await page.route(`**/ingest-sessions/${SESSION}`, (route) =>
    route.fulfill({
      status: 404,
      json: { code: "upload_batch_not_found", message: "导入会话不存在" },
    }),
  )
  await page.route(`**/upload-batches/${SESSION}`, (route) => {
    expect(route.request().method()).toBe("GET")
    legacyReads++
    return route.fulfill({
      json: {
        batch_id: SESSION,
        bc_id: "9876543210987654321",
        status: "available",
        files: [
          {
            material_id: "33333333-3333-4333-8333-000000000000",
            upload_id: "historical-multipart",
            file_name: "历史剧目.mp4",
            byte_size: 8,
            part_size: 8,
            part_count: 1,
            received_bytes: 8,
            status: "available",
            latest_advertiser_id: "9999999999999999999",
            can_retry: true,
          },
        ],
      },
    })
  })
  await page.goto(queueLocation)
  await expect(
    page.getByText("历史批次仅供查看", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("历史剧目.mp4", { exact: true })).toBeVisible()
  await expect(
    page.getByText("9999999999999999999", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "继续传输 / 核实接收" }),
  ).toHaveCount(0)
  await expect(page.getByRole("button", { name: "重试此文件" })).toHaveCount(0)
  expect(legacyReads).toBe(1)
  expect(f.calls.filter((call) => call.method !== "GET")).toHaveLength(0)
})

for (const status of [403, 503])
  test(`session ${status} never falls back to legacy batch`, async ({
    page,
  }) => {
    await ingestBoundary(page)
    let legacyReads = 0
    await page.route(`**/ingest-sessions/${SESSION}`, (route) =>
      route.fulfill({
        status,
        json: {
          code: status === 403 ? "action_forbidden" : "temporarily_unavailable",
          message: "新导入读取失败",
        },
      }),
    )
    await page.route(`**/upload-batches/${SESSION}`, (route) => {
      legacyReads++
      return route.fulfill({ status: 404, json: {} })
    })
    await page.goto(queueLocation)
    await expect(page.getByRole("alert").first()).toBeVisible()
    await expect(
      page.getByText("历史批次仅供查看", { exact: true }),
    ).toHaveCount(0)
    expect(legacyReads).toBe(0)
  })

test("ordinary cancellation waits for direct PUT receipts without aborting or completing the object", async ({
  page,
}) => {
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  const f = await ingestBoundary(page, { putGate: () => gate })
  await begin(page)
  await expect.poll(() => f.puts.length).toBe(2)
  const cancel = page.getByRole("button", { name: "放弃此文件", exact: true })
  try {
    await expect(cancel).toBeEnabled()
    await cancel.click()
    await expect(cancel).toBeDisabled()
    expect(f.calls.filter((c) => c.path.endsWith("/cancel"))).toHaveLength(0)
    expect(f.calls.filter((c) => c.path.endsWith("/complete"))).toHaveLength(0)
    release()
    await expect
      .poll(() => f.calls.filter((c) => c.path.endsWith("/cancel")).length)
      .toBe(1)
    const receipts = f.calls
      .filter((c) => c.path.endsWith("/part-receipts"))
      .flatMap((c) => c.body.receipts)
    expect(receipts).toHaveLength(2)
    expect(receipts.every((r) => r.outcome === "completed" && r.etag)).toBe(
      true,
    )
    expect(
      f.calls.findIndex((c) => c.path.endsWith("/part-receipts")),
    ).toBeLessThan(f.calls.findIndex((c) => c.path.endsWith("/cancel")))
    expect(f.calls.filter((c) => c.path.endsWith("/complete"))).toHaveLength(0)
    expect(f.files.get(0)?.temporary_storage_status).toBe("cleanup_pending")
    expect(f.files.get(0)?.can_retry).toBe(false)
  } finally {
    release()
  }
})

test("lost receipt acknowledgement refresh reuses direct evidence without another PUT", async ({
  page,
}) => {
  const f = await ingestBoundary(page, { lostPartAck: true })
  await begin(page)
  await expect
    .poll(() => f.calls.filter((c) => c.path.endsWith("/part-receipts")).length)
    .toBe(1)
  await expect(
    page.getByRole("button", { name: "继续传输 / 核实接收", exact: true }),
  ).toBeEnabled()
  const first = f.calls.find((c) => c.path.endsWith("/part-receipts"))!.body
  expect(f.puts).toHaveLength(2)
  expect(f.summary.uploaded_count).toBe(0)
  await page.reload()
  await page
    .getByRole("button", { name: "继续传输 / 核实接收", exact: true })
    .click()
  await expect
    .poll(() => f.calls.filter((c) => c.path.endsWith("/part-receipts")).length)
    .toBe(2)
  // Refresh cannot retain File access. Publishing durable HTTP evidence does not
  // manufacture a reselected file or skip the existing hash proof requirement.
  expect(f.summary.uploaded_count).toBe(0)
  await expect(
    page.getByRole("button", { name: "重选此文件", exact: true }),
  ).toBeEnabled()
  await page.getByRole("button", { name: "重选此文件", exact: true }).click()
  await page
    .getByLabel("重选单个原文件", { exact: true })
    .evaluate((input: HTMLInputElement, modified) => {
      const selection = new DataTransfer()
      selection.items.add(
        new File([new Uint8Array(8).fill(1)], "完整剧名_0.mp4", {
          type: "video/mp4",
          lastModified: modified,
        }),
      )
      input.files = selection.files
      input.dispatchEvent(new Event("change", { bubbles: true }))
    }, f.files.get(0)!.last_modified_ms!)
  await expect.poll(() => f.summary.uploaded_count).toBe(1)
  const receipts = f.calls.filter((c) => c.path.endsWith("/part-receipts"))
  expect(receipts).toHaveLength(2)
  expect(receipts[1].body).toEqual(first)
  expect(f.puts).toHaveLength(2)
  expect(f.calls.filter((c) => c.path.endsWith("/part-urls"))).toHaveLength(2)
})

test("forced pause retains unknown PUT permission while later cancellation records intent only", async ({
  page,
}) => {
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  const f = await ingestBoundary(page, { putGate: () => gate })
  await begin(page)
  await expect.poll(() => f.puts.length).toBe(2)
  try {
    const failed = page.waitForEvent("requestfailed", {
      predicate: (r) => r.url().startsWith("https://storage.test/"),
    })
    await page
      .getByRole("button", { name: "暂停本地传输", exact: true })
      .click()
    await failed
    await expect(
      page.getByRole("button", { name: "继续传输 / 核实接收", exact: true }),
    ).toBeEnabled()
    release()
    await page.getByRole("button", { name: "放弃此文件", exact: true }).click()
    await expect
      .poll(() => f.calls.filter((c) => c.path.endsWith("/cancel")).length)
      .toBe(1)
    const receipts = f.calls
      .filter((c) => c.path.endsWith("/part-receipts"))
      .flatMap((c) => c.body.receipts)
    expect(receipts).toHaveLength(2)
    expect(receipts.every((r) => r.outcome === "unknown" && !r.etag)).toBe(true)
    expect(f.files.get(0)?.temporary_storage_status).toBe("cleanup_pending")
    expect(f.files.get(0)?.can_retry).toBe(false)
    expect(f.calls.filter((c) => c.path.endsWith("/complete"))).toHaveLength(0)
  } finally {
    release()
  }
})

for (const failure of ["lostSign", "failFirstPut"] as const) {
  test(`${failure}: signing retries preserve an unused window; every additional PUT has a new permission`, async ({
    page,
  }) => {
    const f = await ingestBoundary(page, { [failure]: true })
    await begin(page)
    await expect
      .poll(() => f.summary.uploaded_count, { timeout: 10000 })
      .toBe(1)
    const signs = f.calls.filter((c) => c.path.endsWith("/part-urls"))
    expect(signs).toHaveLength(3)
    const retried = signs.filter(
      (c) => c.body.part_numbers[0] === signs[0].body.part_numbers[0],
    )
    expect(retried).toHaveLength(2)
    const outcomes = [...f.permissions.values()].map((p) => p.outcome)
    if (failure === "lostSign") {
      expect(retried[1].body.request_id).toBe(retried[0].body.request_id)
      expect(f.permissions.size).toBe(2)
      expect(f.puts).toHaveLength(2)
      expect(outcomes).toEqual(["completed", "completed"])
    } else {
      expect(retried[1].body.request_id).not.toBe(retried[0].body.request_id)
      expect(f.permissions.size).toBe(3)
      expect(f.puts).toHaveLength(3)
      expect(outcomes.filter((o) => o === "unknown")).toHaveLength(1)
      expect(outcomes.filter((o) => o === "completed")).toHaveLength(2)
      const unknown = f.calls
        .filter((c) => c.path.endsWith("/part-receipts"))
        .flatMap((c) => c.body.receipts)
        .filter((r) => r.outcome === "unknown")
      expect(unknown).toHaveLength(1)
      expect(unknown[0].etag).toBeNull()
    }
  })
}

test("cancelling one file pauses the bounded import and other files resume from their saved part proofs", async ({
  page,
}) => {
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  const f = await ingestBoundary(page, { putGate: () => gate })
  await begin(page, 6)
  await expect.poll(() => f.puts.length).toBe(8)
  try {
    await page
      .getByRole("row")
      .filter({ hasText: "完整剧名_0.mp4" })
      .getByRole("button", { name: "放弃此文件", exact: true })
      .click()
    release()
    await expect
      .poll(() => f.calls.filter((c) => c.path.endsWith("/cancel")).length)
      .toBe(1)
    await expect(
      page.getByText(
        "本次传输已暂停。其余未完成文件保留进度，可点“继续传输 / 核实接收”恢复。",
        { exact: true },
      ),
    ).toBeVisible()
    expect(f.puts).toHaveLength(8)
    expect(f.summary.uploaded_count).toBe(0)
    await page
      .getByRole("button", { name: "继续传输 / 核实接收", exact: true })
      .click()
    await expect
      .poll(() => f.summary.uploaded_count, { timeout: 10000 })
      .toBe(5)
    expect(f.puts).toHaveLength(12)
    expect(f.puts.filter((p) => p.index < 4)).toHaveLength(8)
    expect(f.files.get(0)?.temporary_storage_status).toBe("cleanup_pending")
  } finally {
    release()
  }
})

test("queue refresh keeps rows mounted and layout stable without an updating message", async ({
  page,
}) => {
  await ingestBoundary(page, { count: 1, role: "viewer" })
  await page.goto(queueLocation)
  const details = page.getByRole("button", { name: "查看详情", exact: true })
  await expect(details).toBeVisible()
  const originalButton = await details.elementHandle()
  const originalBox = await details.boundingBox()
  let release!: () => void
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  let refreshing = false
  await page.route("**/ingest-sessions/*/files?*", async (route) => {
    refreshing = true
    await gate
    await route.fallback()
  })
  try {
    await page.getByRole("button", { name: "刷新进度", exact: true }).click()
    await expect.poll(() => refreshing).toBe(true)
    await expect(page.getByText("正在更新列表…", { exact: true })).toHaveCount(
      0,
    )
    expect(await originalButton!.evaluate((el) => el.isConnected)).toBe(true)
    expect((await details.boundingBox())!.y).toBe(originalBox!.y)
    await expect(page.getByRole("combobox", { name: "每页条数" })).toBeEnabled()
  } finally {
    release()
  }
  await expect(details).toBeVisible()
})
