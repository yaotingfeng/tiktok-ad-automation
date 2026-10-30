import assert from "node:assert/strict"
import "../src/demo/handlers/ads-management"
import {
  demoWorld,
  MAIN_BC_ID,
  MAIN_TENANT_ID,
} from "../src/demo/fixtures/world"
import { DemoReply, resolveDemoRoute } from "../src/demo/runtime/registry"

// 直接调用演示注册表，不启动真实API，也不拦截或发送广告请求。
const base = `/api/tenants/${MAIN_TENANT_ID}`
async function call(
  method: string,
  path: string,
  body: unknown = {},
  query: Record<string, unknown> = {},
) {
  const route = resolveDemoRoute(method, path)
  assert.ok(route?.handler, `演示入口必须已注册：${method} ${path}`)
  const result = await route.handler({
    operationId: route.id,
    method,
    path: route.params,
    query,
    body,
    search: new URLSearchParams(),
    headers: {},
  } as never)
  return result instanceof DemoReply ? result : new DemoReply(200, result)
}
const actualNow = Date.now
let clock = actualNow()
Date.now = () => clock
try {
  const campaigns = demoWorld.launched
    .filter((campaign) => campaign.bc_id === MAIN_BC_ID)
    .slice(0, 3)
  assert.equal(campaigns.length, 3)
  const frozen = await call(
    "POST",
    `${base}/ad-selections`,
    {
      mode: "EXPLICIT",
      row_keys: campaigns.map(
        (campaign) => `${MAIN_BC_ID}-${campaign.campaign_id}`,
      ),
    },
    { bc_id: MAIN_BC_ID },
  )
  const selection = frozen.body as { selection_id: string }
  const original = campaigns.map((campaign) => campaign.budget)
  const body = {
    selection_id: selection.selection_id,
    bc_id: MAIN_BC_ID,
    mutation: { field: "budget", mode: "add", value: 5 },
    idempotency_key: "demo-direct-arbitrary-key",
    row_problems: [
      { campaign_id: "invalid", new_name: "名称", reason: "id_invalid" },
    ],
  }
  const submitted = await call("POST", `${base}/ad-management-requests`, body)
  assert.equal(submitted.status, 202)
  const task = submitted.body as {
    task_id: string
    status: string
    counts: { prepared: number }
    preparation_problems: unknown[]
  }
  assert.equal(task.status, "PREPARING")
  assert.equal(task.counts.prepared, 0)
  assert.equal(task.preparation_problems.length, 1)
  assert.deepEqual(
    campaigns.map((campaign) => campaign.budget),
    original,
  )
  const duplicate = await call("POST", `${base}/ad-management-requests`, body)
  assert.equal((duplicate.body as typeof task).task_id, task.task_id)
  assert.equal(
    (
      await call("POST", `${base}/ad-management-requests`, {
        ...body,
        mutation: { ...body.mutation, value: 10 },
      })
    ).status,
    409,
  )
  assert.equal(
    (
      await call("POST", `${base}/ad-management-requests`, {
        ...body,
        idempotency_key: "foreign-bc",
        bc_id: "foreign",
      })
    ).status,
    409,
  )
  clock += 4500
  const first = (
    await call(
      "GET",
      `${base}/ad-management-tasks/${task.task_id}`,
      {},
      { offset: 0, limit: 1 },
    )
  ).body as {
    status: string
    items: { ref: { remote_id: string } }[]
    items_total: number
    next_items_offset: number | null
    counts: { accepted: number }
  }
  assert.equal(first.status, "SUCCEEDED")
  assert.equal(first.items.length, 1)
  assert.equal(first.items_total, 3)
  assert.equal(first.next_items_offset, 1)
  assert.equal(first.counts.accepted, 3)
  const last = (
    await call(
      "GET",
      `${base}/ad-management-tasks/${task.task_id}`,
      {},
      { offset: 1, limit: 2 },
    )
  ).body as typeof first
  assert.equal(last.items.length, 2)
  assert.equal(last.next_items_offset, null)
  assert.notEqual(first.items[0].ref.remote_id, last.items[0].ref.remote_id)
  assert.deepEqual(
    campaigns.map((campaign) => campaign.budget),
    original.map((value) => value + 5),
  )
  // 选择过期后，同键重试仍返回原任务，不能二次加预算。
  clock += 31 * 60 * 1000
  assert.equal(
    (
      (await call("POST", `${base}/ad-management-requests`, body))
        .body as typeof task
    ).task_id,
    task.task_id,
  )
  assert.deepEqual(
    campaigns.map((campaign) => campaign.budget),
    original.map((value) => value + 5),
  )
  assert.equal(
    (
      await call("POST", `${base}/ad-management-requests`, {
        ...body,
        idempotency_key: "expired-new-key",
      })
    ).status,
    409,
  )
  const fresh = (
    await call(
      "POST",
      `${base}/ad-selections`,
      {
        mode: "EXPLICIT",
        row_keys: [`${MAIN_BC_ID}-${campaigns[0].campaign_id}`],
      },
      { bc_id: MAIN_BC_ID },
    )
  ).body as typeof selection
  const cancelled = (
    await call("POST", `${base}/ad-management-requests`, {
      ...body,
      selection_id: fresh.selection_id,
      idempotency_key: "cancel-key",
    })
  ).body as typeof task
  assert.equal(
    (
      (
        await call(
          "POST",
          `${base}/ad-management-tasks/${cancelled.task_id}/cancel`,
        )
      ).body as typeof task
    ).status,
    "CANCELLED",
  )
  clock += 4500
  assert.equal(
    (
      (await call("GET", `${base}/ad-management-tasks/${cancelled.task_id}`))
        .body as typeof task
    ).status,
    "CANCELLED",
  )
  assert.equal(campaigns[0].budget, original[0] + 5)
  console.log(
    "demo direct management: submission, progress, idempotency, scope, pagination, row problems and cancellation passed",
  )
} finally {
  Date.now = actualNow
}
