import { expect, test } from "@playwright/test"
import { adsBoundary, BC_A, TENANT } from "./utils/adsBoundary"

const selectionId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
const previewId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
const taskId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

const route = {
  tenant_id: TENANT,
  bc_id: BC_A,
  connection_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  channel: "OFFICIAL_API",
  authorization_revision: 1,
  binding_revision: 1,
  adapter_contract_revision: "test-v1",
}

async function managementBoundary(
  page: Parameters<typeof adsBoundary>[0],
  options: { viewer?: boolean; unsupported?: boolean } = {},
) {
  await adsBoundary(page)
  const previewBodies: Array<Record<string, unknown>> = []
  await page.route("**/api/**", async (handler) => {
    const request = handler.request()
    const url = new URL(request.url())
    const path = url.pathname
    if (options.viewer && path === "/api/me/tenants")
      return handler.fulfill({
        json: {
          items: [
            {
              id: TENANT,
              name: "报表租户",
              active: true,
              role: "viewer",
              default_bc_id: BC_A,
            },
          ],
          next_cursor: null,
          total: 1,
        },
      })
    if (path.endsWith(`/tenants/${TENANT}/ad-selections`))
      return handler.fulfill({
        json: {
          selection_id: selectionId,
          snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          refs: [
            {
              tenant_id: TENANT,
              advertiser_id: "adv-1",
              kind: "campaign",
              remote_id: "campaign-1",
            },
          ],
          material_uses: [],
          membership_digest: "m".repeat(64),
          expires_at: "2099-01-01T00:00:00Z",
        },
      })
    if (
      request.method() === "POST" &&
      path.endsWith(`/tenants/${TENANT}/ad-management-previews`)
    ) {
      const body = request.postDataJSON() as {
        mutation?: { excluded_refs?: unknown[]; include_parents?: unknown[] }
      }
      previewBodies.push(body)
      const excluded = body.mutation?.excluded_refs ?? []
      return handler.fulfill({
        status: 201,
        json: {
          preview_id: previewId,
          digest: "p".repeat(64),
          created_at: "2026-10-01T00:00:00Z",
          expires_at: "2099-01-01T00:05:00Z",
          bc_id: BC_A,
          route,
          items:
            options.unsupported && excluded.length === 0
              ? [
                  {
                    ref: {
                      tenant_id: TENANT,
                      advertiser_id: "adv-1",
                      kind: "campaign",
                      remote_id: "campaign-1",
                    },
                    original_value: "20",
                    final_value: "25",
                    execution_result: "UNSUPPORTED",
                    reason: "unsupported",
                  },
                ]
              : [],
          counts: {
            selected: 1,
            targets: 1,
            linked: 0,
            unsupported: options.unsupported && excluded.length === 0 ? 1 : 0,
          },
        },
      })
    }
    if (
      request.method() === "POST" &&
      path.endsWith(`/tenants/${TENANT}/ad-management-tasks`)
    )
      return handler.fulfill({
        status: 202,
        json: {
          task_id: taskId,
          bc_id: BC_A,
          status: "QUEUED",
          counts: { selected: 1, targets: 1, linked: 0, unsupported: 0 },
        },
      })
    if (
      request.method() === "POST" &&
      path.endsWith(`/ad-management-tasks/${taskId}/refresh`)
    )
      return handler.fulfill({
        status: 202,
        json: {
          task_id: taskId,
          bc_id: BC_A,
          status: "RUNNING",
          counts: { selected: 1, targets: 1, refresh_pending: 1 },
        },
      })
    if (request.method() === "GET" && path.endsWith("/ad-management-tasks"))
      return handler.fulfill({
        json: {
          items: [
            {
              task_id: taskId,
              bc_id: BC_A,
              status: "QUEUED",
              counts: { selected: 1, targets: 1, linked: 0, unsupported: 0 },
            },
          ],
          next_cursor: null,
          total: 1,
        },
      })
    if (
      request.method() === "GET" &&
      path.endsWith(`/ad-management-tasks/${taskId}`)
    )
      return handler.fulfill({
        json: {
          task_id: taskId,
          bc_id: BC_A,
          status: "QUEUED",
          counts: {
            selected: 1,
            targets: 1,
            linked: 0,
            unsupported: 0,
            refresh_pending: 1,
          },
          items: [
            {
              ref: {
                tenant_id: TENANT,
                advertiser_id: "adv-1",
                kind: "campaign",
                remote_id: "campaign-1",
              },
              execution_result: "ACCEPTED",
              delivery_status: "DELIVERED",
              observation_state: "REFRESH_PENDING",
              attempts: [
                {
                  attempt: 1,
                  request_at: "2026-10-01T00:00:00Z",
                  outcome: "ACCEPTED",
                  request_id: "req-1",
                },
              ],
            },
          ],
        },
      })
    return handler.fallback()
  })
  return { previewBodies }
}

test.describe("广告管理工作台", () => {
  test("当前选择打开预览并提交后进入任务详情", async ({ page }) => {
    const { previewBodies } = await managementBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await page
      .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" })
      .check()
    await page.getByRole("button", { name: "暂停", exact: true }).click()
    await expect(page.getByRole("heading", { name: "批量暂停" })).toBeVisible()
    await expect.poll(() => previewBodies.length).toBe(1)
    expect(previewBodies[0]?.mutation).toMatchObject({
      field: "status",
      mode: "set",
      value: "DISABLE",
    })
    await expect(page.getByText(`连接：${route.connection_id}`)).toBeVisible()
    await expect(page.getByText("账户：adv-1")).toBeVisible()
    // 后台新发布只提示，不关闭用户正在确认的冻结管理预览。
    await page.route("**/ad-sync-status?*", (handler) =>
      handler.fulfill({
        json: {
          accounts: [],
          latest_directory_at: null,
          publication_version: "background-new",
          plan: {
            enabled: true,
            bc_report_supported: true,
            next_hot_at: null,
            next_daily_at: null,
            slowed: false,
          },
        },
      }),
    )
    await page.clock.install()
    await page.clock.fastForward(31_000)
    await expect(page.getByRole("heading", { name: "批量暂停" })).toBeVisible()
    await page.getByRole("checkbox", { name: "包括父级联动" }).click()
    await expect.poll(() => previewBodies.length).toBe(2)
    expect(previewBodies[1]?.mutation?.include_parents).toEqual([
      {
        tenant_id: TENANT,
        advertiser_id: "adv-1",
        kind: "campaign",
        remote_id: "campaign-1",
      },
    ])
    await expect(page.getByText("五分钟内有效")).toBeVisible()
    await page.getByRole("button", { name: "提交管理任务" }).click()
    await expect(page).toHaveURL(
      new RegExp(`/tenants/${TENANT}/ad-management-tasks/${taskId}`),
    )
    await expect(
      page.getByRole("heading", { name: "管理任务详情" }),
    ).toBeVisible()
    await expect(page.getByText("REFRESH_PENDING")).toBeVisible()
    await page.getByRole("button", { name: /刷新重试/ }).click()
    await expect(page.getByText("已排队重试定向刷新")).toBeVisible()
  })

  test("任务列表显示状态且 viewer 隐藏写入口", async ({ page }) => {
    await managementBoundary(page, { viewer: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await page
      .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" })
      .check()
    // 只读成员只看到计数和取消选择，不出现任何写入动作。
    await expect(page.getByText("已选 1 条")).toBeVisible()
    await expect(page.getByRole("button", { name: "取消选择" })).toBeVisible()
    for (const name of ["启用", "暂停", "修改目标 ROAS", "修改预算"])
      await expect(page.getByRole("button", { name, exact: true })).toHaveCount(
        0,
      )
    await page.goto(`/tenants/${TENANT}/ad-management-tasks?bc_id=${BC_A}`)
    await expect(page.getByRole("heading", { name: "管理任务" })).toBeVisible()
    await expect(page.getByText("排队中")).toBeVisible()
    await page.getByRole("link", { name: /dddddddd/ }).click()
    await expect(
      page.getByText("当前为只读成员，隐藏取消、重试和恢复入口。"),
    ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "取消未发送项" }),
    ).toHaveCount(0)
    await expect(page.getByRole("button", { name: /刷新重试/ })).toHaveCount(0)
  })

  test("排除不可用项会把冻结 ref 发送给服务器", async ({ page }) => {
    const { previewBodies } = await managementBoundary(page, {
      unsupported: true,
    })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await page
      .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" })
      .check()
    await page.getByRole("button", { name: "修改预算" }).click()
    await expect(page.getByRole("heading", { name: "修改预算" })).toBeVisible()
    await page.getByRole("textbox", { name: "管理新值" }).fill("25")
    await expect(page.getByText(/排除不可用项后才可提交/)).toBeVisible()
    await expect(
      page.getByRole("button", { name: "提交管理任务" }),
    ).toBeDisabled()
    await page.getByRole("checkbox", { name: "排除不可用项" }).click()
    await expect.poll(() => previewBodies.length).toBe(2)
    expect(previewBodies[1]?.mutation?.excluded_refs).toEqual([
      {
        tenant_id: TENANT,
        advertiser_id: "adv-1",
        kind: "campaign",
        remote_id: "campaign-1",
      },
    ])
    await expect(
      page.getByRole("button", { name: "提交管理任务" }),
    ).toBeEnabled()
  })

  test("勾选后表头直接出现动作，启用提交 ENABLE", async ({ page }) => {
    await managementBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await page
      .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" })
      .check()
    for (const name of [
      "启用",
      "暂停",
      "修改目标 ROAS",
      "修改预算",
      "取消选择",
    ])
      await expect(
        page.getByRole("button", { name, exact: true }),
      ).toBeVisible()
    await expect(page.getByRole("button", { name: "批量操作" })).toHaveCount(0)
    const preview = page.waitForRequest(
      (r) =>
        r.url().includes("ad-management-previews") && r.method() === "POST",
    )
    await page.getByRole("button", { name: "启用", exact: true }).click()
    expect((await preview).postDataJSON().mutation).toMatchObject({
      field: "status",
      mode: "set",
      value: "ENABLE",
    })
    await expect(page.getByRole("heading", { name: "批量启用" })).toBeVisible()
    await expect(page.getByRole("combobox", { name: "操作维度" })).toHaveCount(
      0,
    )
  })

  test("修改目标 ROAS 支持按百分比增加，数值为空不生成预览", async ({
    page,
  }) => {
    const { previewBodies } = await managementBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告组", exact: true }).click()
    await page.getByRole("checkbox").nth(1).check()
    await page.getByRole("button", { name: "修改目标 ROAS" }).click()
    await expect(
      page.getByRole("heading", { name: "修改目标 ROAS" }),
    ).toBeVisible()
    await expect(page.getByText("请输入数值")).toBeVisible()
    await expect(page.getByRole("status", { name: /正在生成/ })).toHaveCount(0)
    await page.getByRole("combobox", { name: "调整方式" }).click()
    await page.getByRole("option", { name: "增加百分比" }).click()
    // 空值或非法数值都不能发出预览请求，避免默认值误改真实广告。
    await page.getByRole("textbox", { name: "管理新值" }).fill("abc")
    await page.waitForTimeout(700)
    expect(previewBodies).toHaveLength(0)
    const preview = page.waitForRequest((r) =>
      r.url().includes("ad-management-previews"),
    )
    await page.getByRole("textbox", { name: "管理新值" }).fill("10")
    expect((await preview).postDataJSON().mutation).toMatchObject({
      field: "roas",
      mode: "increase_percent",
      value: "10",
    })
  })

  test("账户维度不提供修改目标 ROAS", async ({ page }) => {
    await managementBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("checkbox").nth(1).check()
    for (const name of ["启用", "暂停", "修改预算"])
      await expect(
        page.getByRole("button", { name, exact: true }),
      ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "修改目标 ROAS" }),
    ).toHaveCount(0)
  })

  test("取消选择恢复列设置，选择全部匹配结果在工具栏", async ({ page }) => {
    await managementBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await expect(page.getByRole("button", { name: "列设置" })).toBeVisible()
    await page
      .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" })
      .check()
    await page.getByRole("button", { name: "选择全部匹配结果" }).click()
    await expect(page.getByText(/已选全部 \d+ 条匹配结果/)).toBeVisible()
    await expect(
      page.getByRole("button", { name: "选择全部匹配结果" }),
    ).toBeDisabled()
    await page.getByRole("button", { name: "取消选择" }).click()
    await expect(page.getByRole("button", { name: "列设置" })).toBeVisible()
    await expect(
      page.getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" }),
    ).not.toBeChecked()
  })
})
