import { createHash } from "node:crypto"
import { expect, test } from "@playwright/test"

test("real login and ingest APIs receive browser files through a loopback HTTPS R2 transport", async ({
  page,
  request,
}) => {
  const seeded = await request.post("/__r2_acceptance__/scenario")
  expect(seeded.ok()).toBeTruthy()
  const scope = await seeded.json()
  const clipResponse = await request.get("/__r2_acceptance__/video")
  expect(clipResponse.ok()).toBeTruthy()
  const clip = await clipResponse.body()
  // A valid ISO BMFF free box makes a real multipart file without fake media bytes.
  const free = Buffer.alloc(16 * 1024 * 1024)
  free.writeUInt32BE(free.length, 0)
  free.write("free", 4, "ascii")
  const large = Buffer.concat([clip, free])
  const chosen = [clip, clip, large].map((buffer, index) => ({
    name: `验收完整剧名_${index}.mp4`,
    mimeType: "video/mp4",
    buffer,
  }))
  const failures: string[] = []
  page.on("pageerror", (error) => failures.push(error.message))
  const writes: { method: string; path: string }[] = []
  page.on("request", (r) => {
    if (r.method() !== "GET")
      writes.push({ method: r.method(), path: new URL(r.url()).pathname })
  })
  await page.goto(`/tenants/${scope.tenant_id}/materials?bc_id=${scope.bc_id}`)
  await page.getByLabel("账号", { exact: true }).fill(scope.username)
  await page.getByLabel("密码", { exact: true }).fill(scope.password)
  await page.getByRole("button", { name: "登录 TT ADA", exact: true }).click()
  await expect(
    page.getByRole("heading", { name: "素材库", exact: true }),
  ).toBeVisible()
  await expect(page.getByText(scope.bc_id, { exact: true })).toBeVisible()
  await page
    .getByRole("button", { name: "批量上传", exact: true })
    .first()
    .click()
  const sheet = page.getByRole("dialog", { name: "批量上传素材", exact: true })
  await sheet.getByLabel("选择本地素材").setInputFiles(chosen)
  await sheet
    .getByRole("button", { name: "开始上传 3 个文件", exact: true })
    .click()
  await expect(page).toHaveURL(/batch_id=[0-9a-f-]{36}/)
  await expect(
    page.getByText("已登记 3 · 已接收 3 · 平台可用 0 · 已清理 0 · 失败 0", {
      exact: true,
    }),
  ).toBeVisible({ timeout: 45_000 })
  const sessionId = new URL(page.url()).searchParams.get("batch_id")!
  const facts = await page.evaluate(
    async ({ tenant, session }) => {
      const headers = {
        Authorization: `Bearer ${localStorage.getItem("access_token")}`,
      }
      const base = `/api/tenants/${tenant}/materials/ingest-sessions/${session}`
      const summary = await fetch(base, { headers })
      const files = await fetch(`${base}/files?limit=100`, { headers })
      return {
        summaryStatus: summary.status,
        filesStatus: files.status,
        summary: await summary.json(),
        files: await files.json(),
      }
    },
    { tenant: scope.tenant_id, session: sessionId },
  )
  expect(facts.summaryStatus).toBe(200)
  expect(facts.filesStatus).toBe(200)
  expect(facts.summary).toMatchObject({
    accepted_count: 3,
    uploaded_count: 3,
    ready_count: 0,
    cleaned_count: 0,
  })
  expect(facts.files.items).toHaveLength(3)
  expect(facts.files.next_cursor).toBeNull()
  expect(
    facts.files.items
      .map((f: { received_bytes: number }) => f.received_bytes)
      .sort((a: number, b: number) => a - b),
  ).toEqual([clip.length, clip.length, large.length])
  const evidenceResponse = await request.get(
    `/__r2_acceptance__/evidence/${scope.tenant_id}`,
  )
  expect(evidenceResponse.ok()).toBeTruthy()
  const evidence = await evidenceResponse.json()
  expect(evidence).toMatchObject({
    session_count: 1,
    material_count: 3,
    stored_count: 3,
    validator_dispatch_count: 3,
    available_asset_count: 0,
    received_bytes: clip.length * 2 + large.length,
    successful_puts: 4,
    completed_objects: 3,
    credential_header_violations: 0,
  })
  expect(evidence.object_sha256.sort()).toEqual(
    chosen
      .map(({ buffer }) => createHash("sha256").update(buffer).digest("hex"))
      .sort(),
  )
  expect(evidence.permission_outcomes).toEqual({ completed: 4 })
  expect(
    writes.filter((r) => r.path.endsWith("/ingest-sessions")),
  ).toHaveLength(1)
  expect(writes.filter((r) => r.path.endsWith("/chunks"))).toHaveLength(1)
  expect(writes.filter((r) => r.path.endsWith("/complete"))).toHaveLength(3)
  expect(writes.filter((r) => r.path.includes("/upload-batches"))).toHaveLength(
    0,
  )
  await page.reload()
  await expect(
    page.getByText("已登记 3 · 已接收 3 · 平台可用 0 · 已清理 0 · 失败 0", {
      exact: true,
    }),
  ).toBeVisible()
  expect(writes.filter((r) => r.path.endsWith("/complete"))).toHaveLength(3)
  const forbidden = await page.evaluate(
    async ({ tenant, session }) => {
      const response = await fetch(
        `/api/tenants/${tenant}/materials/ingest-sessions/${session}`,
        {
          headers: {
            Authorization: `Bearer ${localStorage.getItem("access_token")}`,
          },
        },
      )
      return response.status
    },
    { tenant: scope.other_tenant_id, session: sessionId },
  )
  expect([403, 404]).toContain(forbidden)
  expect(failures).toEqual([])
  console.info(`R2 browser acceptance: ${JSON.stringify(evidence)}`)
})
