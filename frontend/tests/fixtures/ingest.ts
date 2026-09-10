import { expect, type Page } from "@playwright/test"
import type {
  IngestFilePublic,
  IngestPartPermission,
  IngestSummary,
} from "../../src/client"
export const TENANT = "11111111-1111-4111-8111-111111111111"
export const TENANT_B = "22222222-2222-4222-8222-222222222222"
export const BC = "9876543210987654321"
export const BC_B = "9876543210987654322"
export const SESSION = "44444444-4444-4444-8444-444444444444"
export const USER = "55555555-5555-4555-8555-555555555555"
export const location = `/tenants/${TENANT}/materials?bc_id=${BC}`
export const queueLocation = `${location}&tab=uploads&batch_id=${SESSION}`
export const video = (index: number, size = 8) => ({
  name: `完整剧名_${index}.mp4`,
  mimeType: "video/mp4",
  buffer: Buffer.alloc(size, index + 1),
})
export async function ingestBoundary(
  page: Page,
  options: {
    role?: string
    count?: number
    lostCreate?: boolean
    lostChunk?: boolean
    lostComplete?: boolean
    completingPages?: number
    backpressure?: boolean
    denySign?: boolean
    putGate?: () => Promise<void>
    lostPartAck?: boolean
    lostSign?: boolean
    failFirstPut?: boolean
  } = {},
) {
  const calls: {
    path: string
    method: string
    body: any
    query: URLSearchParams
  }[] = []
  const permissions = new Map<string, IngestPartPermission>()
  let lostPartAck = !!options.lostPartAck
  let lostSign = !!options.lostSign
  let failFirstPut = !!options.failFirstPut
  const puts: { index: number; part: number; bytes: number }[] = []
  const files = new Map<number, IngestFilePublic>()
  const parts = new Map<
    number,
    Map<number, { part_number: number; byte_size: number; etag: string }>
  >()
  const summary: IngestSummary = {
    session_id: SESSION,
    bc_id: BC,
    expected_count: options.count ?? 0,
    total_bytes: (options.count ?? 0) * 8,
    status: "registering",
    registration_cursor: -1,
    accepted_count: options.count ?? 0,
    uploaded_count: 0,
    ready_count: 0,
    failed_count: 0,
    cleaned_count: 0,
    reserved_bytes: 0,
    stored_bytes: 0,
    created_at: "2026-09-10T08:00:00Z",
  }
  const createFile = (
    index: number,
    input = {
      file_name: `完整剧名_${index}.mp4`,
      size: 8,
      mime_type: "video/mp4",
      last_modified_ms: 123,
    },
  ) =>
    ({
      material_id: `33333333-3333-4333-8333-${String(index).padStart(12, "0")}`,
      client_index: index,
      file_name: input.file_name,
      size: input.size,
      mime_type: input.mime_type,
      last_modified_ms: input.last_modified_ms,
      generation: 1,
      upload_id: null,
      part_size: 4,
      part_count: Math.ceil(input.size / 4),
      operation_revision: 0,
      platform_status: "receiving",
      temporary_storage_status: "missing",
      received_bytes: 0,
      can_retry: false,
      source_advertiser_id: null,
      error_code: null,
      operation_status: "idle",
      task_id: null,
    }) satisfies IngestFilePublic
  for (let index = 0; index < (options.count ?? 0); index++)
    files.set(index, createFile(index))
  let requestId: string | null = null
  let lostChunk = !!options.lostChunk
  let lostCreate = !!options.lostCreate,
    lostComplete = !!options.lostComplete
  let backpressure = !!options.backpressure
  const completionPages = new Map<number, number>()
  const uploadedOnce = new Set<number>()
  const chunks = new Map<string, number[]>()
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("ingest-test-initialized")) {
      localStorage.setItem("access_token", "ingest-token")
      sessionStorage.setItem("ingest-test-initialized", "true")
    }
  })
  await page.route("**/api/**", async (route) => {
    const request = route.request(),
      url = new URL(request.url()),
      path = url.pathname,
      method = request.method(),
      body = request.postData() ? request.postDataJSON() : undefined
    // A shared read query can be re-mounted while logout clears the cache; the
    // real application returns 401. Transfer callbacks and every write remain fenced.
    if (
      !request.headers().authorization &&
      method === "GET" &&
      !/\/files\/[^/]+$/.test(path)
    )
      return route.fulfill({
        status: 401,
        json: { detail: "Not authenticated" },
      })
    expect(request.headers().authorization).toBe("Bearer ingest-token")
    calls.push({ path, method, body, query: url.searchParams })
    const reply = (json: unknown, status = 200) =>
      route.fulfill({ json, status })
    if (path === "/api/users/me")
      return reply({
        id: USER,
        username: "operator",
        full_name: "素材用户",
        is_active: true,
        is_superuser: false,
      })
    if (path === "/api/me/tenants")
      return reply({
        items: [
          {
            id: TENANT,
            name: "租户甲",
            active: true,
            role: options.role ?? "operator",
          },
          {
            id: TENANT_B,
            name: "租户乙",
            active: true,
            role: options.role ?? "operator",
          },
        ].filter(
          (t) =>
            !url.searchParams.get("search") ||
            t.id === url.searchParams.get("search"),
        ),
        next_cursor: null,
      })
    if (path.endsWith("/bcs"))
      return reply({
        items: [
          { bc_id: BC, name: "素材 BC 甲", status: "ACTIVE" },
          { bc_id: BC_B, name: "素材 BC 乙", status: "ACTIVE" },
        ],
        next_cursor: null,
      })
    if (path.endsWith("/materials"))
      return reply({ items: [], next_cursor: null })
    if (!path.startsWith(`/api/tenants/${TENANT}/materials/`))
      return reply({ code: "scope_forbidden" }, 403)
    if (path.endsWith("/ingest-sessions")) {
      if (method === "GET")
        return reply({
          items: summary.expected_count ? [summary] : [],
          next_cursor: null,
        })
      if (requestId) expect(body.request_id).toBe(requestId)
      requestId = body.request_id
      expect(body).toEqual({
        bc_id: BC,
        request_id: requestId,
        file_count: expect.any(Number),
        total_bytes: expect.any(Number),
      })
      summary.expected_count = body.file_count
      summary.total_bytes = body.total_bytes
      if (lostCreate) {
        lostCreate = false
        return route.abort("failed")
      }
      return reply(summary, 201)
    }
    if (path.includes("/ingest-requests/"))
      return requestId && path.endsWith(requestId)
        ? reply(summary)
        : reply({ code: "upload_batch_not_found" }, 404)
    if (path.endsWith(`/ingest-sessions/${SESSION}`)) return reply(summary)
    if (path.endsWith("/chunks") && method === "POST") {
      expect(body.files.length).toBeLessThanOrEqual(200)
      if (!chunks.has(body.request_id)) {
        chunks.set(
          body.request_id,
          body.files.map((f: any) => f.client_index),
        )
        for (const input of body.files)
          files.set(input.client_index, createFile(input.client_index, input))
        summary.accepted_count = files.size
        summary.registration_cursor = files.size - 1
      }
      if (lostChunk) {
        lostChunk = false
        return route.abort("failed")
      }
      return reply(
        {
          session_id: SESSION,
          request_id: body.request_id,
          items: chunks.get(body.request_id)!.map((index) => files.get(index)),
        },
        201,
      )
    }
    if (path.endsWith("/seal")) {
      summary.status = "sealed"
      return reply({
        sealed: files.size === summary.expected_count,
        issues: [],
        summary,
      })
    }
    if (path.endsWith("/files")) {
      const limit = Number(url.searchParams.get("limit") ?? 100),
        offset = Number(url.searchParams.get("cursor") ?? 0)
      expect(limit).toBeLessThanOrEqual(100)
      const rows = [...files.values()].filter(
        (f) =>
          !url.searchParams.get("status") ||
          (url.searchParams.get("status") === "registered"
            ? f.temporary_storage_status === "missing"
            : url.searchParams.get("status") === "failed"
              ? f.platform_status === "blocked"
              : f.temporary_storage_status === url.searchParams.get("status")),
      )
      return reply({
        items: rows.slice(offset, offset + limit),
        next_cursor:
          offset + limit < rows.length ? String(offset + limit) : null,
      })
    }
    const id = path.match(/\/files\/([^/]+)/)?.[1]
    const file = [...files.values()].find((file) => file.material_id === id)
    if (!file) return reply({ code: "fixture_missing", message: path }, 404)
    if (method === "GET" && path.endsWith(file.material_id)) return reply(file)
    const identity = () => ({
      generation: file.generation,
      upload_id: file.upload_id!,
      operation_revision: file.operation_revision,
    })
    if (path.endsWith("/resume")) {
      if (backpressure) {
        file.temporary_storage_status = "waiting_capacity"
        return reply(file)
      }
      if (!file.upload_id) {
        file.upload_id = `multipart-${file.client_index}`
        file.operation_revision++
        file.temporary_storage_status = "receiving"
      }
      return reply(file)
    }
    if (path.endsWith("/parts")) {
      expect(Number(url.searchParams.get("operation_revision"))).toBe(
        file.operation_revision,
      )
      return reply({
        ...identity(),
        items: [...(parts.get(file.client_index)?.values() ?? [])],
        next_cursor: null,
      })
    }
    if (path.endsWith("/part-urls")) {
      if (options.denySign) return reply({ code: "action_forbidden" }, 403)
      expect(body.operation_revision).toBe(file.operation_revision)
      expect(body.request_id).toMatch(/^[0-9a-f-]{36}$/)
      expect(body.part_numbers.length).toBeLessThanOrEqual(2)
      const key = `${file.material_id}:${body.request_id}`
      if (!permissions.has(key))
        permissions.set(key, {
          part_number: body.part_numbers[0],
          permission_id: crypto.randomUUID(),
          permission_nonce: crypto.randomUUID(),
          permission_revision: 1,
          outcome: "signed",
        })
      if (lostSign) {
        lostSign = false
        return route.abort("failed")
      }
      return reply({
        ...identity(),
        items: body.part_numbers.map((part: number) => ({
          ...permissions.get(key),
          part_number: part,
          byte_size: Math.min(
            file.part_size,
            file.size - (part - 1) * file.part_size,
          ),
          url: `https://storage.test/object/${file.client_index}/${part}?signature=ephemeral`,
          expires_in: 300,
        })),
      })
    }
    if (path.endsWith("/part-permissions")) {
      const permission = permissions.get(
        `${file.material_id}:${url.searchParams.get("request_id")}`,
      )
      return reply({
        ...identity(),
        request_id: url.searchParams.get("request_id"),
        items: permission ? [permission] : [],
      })
    }
    if (path.endsWith("/part-receipts")) {
      expect(body.receipts.length).toBeGreaterThan(0)
      expect(body.receipts.length).toBeLessThanOrEqual(2)
      for (const receipt of body.receipts) {
        const permission = [...permissions.entries()].find(
          ([key, p]) =>
            key.startsWith(`${file.material_id}:`) &&
            p.permission_id === receipt.permission_id,
        )?.[1]
        expect(permission).toBeDefined()
        expect(receipt.permission_nonce).toBe(permission!.permission_nonce)
        expect(receipt.permission_revision).toBe(
          permission!.permission_revision,
        )
        expect(receipt.part_number).toBe(permission!.part_number)
        expect(["signed", receipt.outcome]).toContain(permission!.outcome)
        if (receipt.outcome === "completed")
          expect(receipt.etag).toBe(
            parts.get(file.client_index)?.get(receipt.part_number)?.etag,
          )
        permission!.outcome = receipt.outcome
      }
      if (lostPartAck) {
        lostPartAck = false
        return route.abort("failed")
      }
      return reply({
        ...identity(),
        accepted_permission_ids: body.receipts.map((r: any) => r.permission_id),
      })
    }
    if (path.endsWith("/cancel")) {
      expect(body).toEqual(identity())
      file.platform_status = "blocked"
      file.temporary_storage_status = file.upload_id
        ? "cleanup_pending"
        : "waiting_capacity"
      file.can_retry = !file.upload_id
      file.operation_revision++
      return reply(file)
    }
    if (path.endsWith("/new-generation")) {
      expect(body).toEqual(identity())
      expect(file.can_retry).toBe(true)
      file.generation++
      file.operation_revision++
      file.upload_id = null
      file.received_bytes = 0
      file.can_retry = false
      file.platform_status = "receiving"
      file.temporary_storage_status = "missing"
      parts.delete(file.client_index)
      return reply(file)
    }
    if (path.endsWith("/complete")) {
      expect(body).toEqual(identity())
      const done = completionPages.get(file.client_index) ?? 0
      completionPages.set(file.client_index, done + 1)
      file.operation_revision++
      if (done < (options.completingPages ?? 0)) {
        file.operation_status = "completing"
        return reply(file)
      }
      expect(parts.get(file.client_index)?.size).toBe(file.part_count)
      file.received_bytes = file.size
      file.temporary_storage_status = "stored"
      file.platform_status = "stored"
      file.operation_status = "idle"
      if (!uploadedOnce.has(file.client_index)) {
        summary.uploaded_count++
        uploadedOnce.add(file.client_index)
      }
      summary.stored_bytes += file.size
      if (lostComplete) {
        lostComplete = false
        return route.abort("failed")
      }
      return reply(file)
    }
    return reply({ code: "fixture_missing", message: path }, 404)
  })
  await page.route("https://storage.test/**", async (route) => {
    const request = route.request(),
      match = new URL(request.url()).pathname.match(/\/object\/(\d+)\/(\d+)/)!
    expect(request.method()).toBe("PUT")
    expect(request.headers().authorization).toBeUndefined()
    expect(request.headers().cookie).toBeUndefined()
    expect(request.headers().referer).toBeUndefined()
    const index = Number(match[1]),
      part = Number(match[2]),
      bytes = request.postDataBuffer()!.length
    puts.push({ index, part, bytes })
    await options.putGate?.()
    if (failFirstPut) {
      failFirstPut = false
      return route.fulfill({ status: 503 })
    }
    const record = parts.get(index) ?? new Map()
    record.set(part, {
      part_number: part,
      byte_size: bytes,
      etag: `"etag-${index}-${part}"`,
    })
    parts.set(index, record)
    await route.fulfill({
      status: 200,
      headers: {
        ETag: `"etag-${index}-${part}"`,
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "ETag",
      },
    })
  })
  return {
    calls,
    permissions,
    puts,
    files,
    parts,
    summary,
    capacityAvailable: () => {
      backpressure = false
    },
  }
}
