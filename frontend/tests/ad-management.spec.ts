import { expect, test } from "@playwright/test"
import {
  parsePastedRows,
  renameRuleFrom,
} from "../src/features/ad-management/renameRules"
import { adsBoundary, BC_A, TENANT } from "./utils/adsBoundary"

const selectionId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
const previewId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
const taskId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
const ruleTaskId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

const CAMPAIGN_REF = {
  tenant_id: TENANT,
  advertiser_id: "adv-1",
  kind: "campaign",
  remote_id: "campaign-1",
}
const GROUP_REF = {
  tenant_id: TENANT,
  advertiser_id: "adv-1",
  kind: "adgroup",
  remote_id: "group-zero",
}
const PARENT_REF = {
  tenant_id: TENANT,
  advertiser_id: "adv-1",
  kind: "campaign",
  remote_id: "campaign-paused",
}

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
  options: {
    viewer?: boolean
    unsupported?: boolean
    blockedParent?: boolean
    previewError?: { status: number; code: string; message: string }
  } = {},
) {
  await adsBoundary(page)
  const previewBodies: Array<Record<string, unknown>> = []
  const listQueries: Array<Record<string, string>> = []
  const renameSelectionBodies: Array<Record<string, unknown>> = []
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
    // 表格改名解析：第一行命中 campaign-1，第二行在本 BC 目录里找不到。
    if (
      request.method() === "POST" &&
      path.endsWith(`/tenants/${TENANT}/ad-management-rename-selections`)
    ) {
      renameSelectionBodies.push(request.postDataJSON())
      return handler.fulfill({
        json: {
          selection: {
            selection_id: selectionId,
            snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            refs: [CAMPAIGN_REF],
            material_uses: [],
            membership_digest: "m".repeat(64),
            expires_at: "2099-01-01T00:00:00Z",
          },
          problems: [
            { campaign_id: "1799999999999999999", reason: "not_found" },
          ],
        },
      })
    }
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
        mutation?: {
          field?: string
          excluded_refs?: unknown[]
          include_parents?: unknown[]
        }
      }
      previewBodies.push(body)
      if (options.previewError)
        return handler.fulfill({
          status: options.previewError.status,
          json: {
            code: options.previewError.code,
            message: options.previewError.message,
            retryable: false,
          },
        })
      const excluded = body.mutation?.excluded_refs ?? []
      const parents = body.mutation?.include_parents ?? []
      // 默认选中的系列是可修改项；不可用、暂停上级两种场景各自追加对应行。
      // 改名预览返回新旧名称，并提示改名后剧名发生变化。
      const items: Array<Record<string, unknown>> = [
        body.mutation?.field === "name"
          ? {
              ref: CAMPAIGN_REF,
              name: "嘉书-总裁归来-测试",
              original_value: "嘉书-总裁归来-测试",
              final_value: "嘉书-剧A-1015",
              execution_result: "PENDING",
              reason: null,
              drama_name: "剧A",
              provider_label: "嘉书",
              drama_changed: true,
            }
          : {
              ref: CAMPAIGN_REF,
              name: "嘉书-总裁归来-测试",
              original_value: "20",
              final_value: "25",
              execution_result: "PENDING",
              reason: null,
            },
      ]
      if (options.unsupported && excluded.length === 0)
        items.push({
          ref: GROUP_REF,
          name: "预算为零的广告组",
          original_value: null,
          final_value: null,
          execution_result: "UNSUPPORTED",
          reason: "configuration_invalid",
        })
      if (options.unsupported && excluded.length > 0)
        items.push({
          ref: GROUP_REF,
          execution_result: "UNSUPPORTED",
          reason: "excluded_ref",
        })
      if (options.blockedParent)
        items.push({
          ref: PARENT_REF,
          name: "暂停中的上级系列",
          final_value: "ENABLE",
          execution_result: parents.length ? "PENDING" : "NO_CHANGE",
          reason: parents.length ? null : "parent_disabled",
        })
      return handler.fulfill({
        status: 201,
        json: {
          preview_id: previewId,
          digest: "p".repeat(64),
          created_at: "2026-10-01T00:00:00Z",
          expires_at: "2099-01-01T00:05:00Z",
          bc_id: BC_A,
          route,
          items,
          counts: {
            selected: 1,
            targets: items.filter((i) => i.execution_result === "PENDING")
              .length,
            linked: 0,
            unsupported: items.filter(
              (i) => i.execution_result === "UNSUPPORTED",
            ).length,
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
    if (request.method() === "GET" && path.endsWith("/ad-management-tasks")) {
      const query = new URL(request.url()).searchParams
      listQueries.push(Object.fromEntries(query))
      const rows = [
        {
          task_id: taskId,
          bc_id: BC_A,
          status: "QUEUED",
          counts: { selected: 1, targets: 1, linked: 0, unsupported: 0 },
          created_at: "2026-10-14T02:30:00Z",
          actor_name: "投放同学",
          mutation: { field: "status", mode: "set", value: "DISABLE" },
          source: { kind: "manual" },
          target_kinds: { campaign: 1 },
        },
        {
          task_id: ruleTaskId,
          bc_id: BC_A,
          status: "PARTIAL",
          counts: { selected: 3, targets: 3, accepted: 2, rejected: 1 },
          created_at: "2026-10-14T01:00:00Z",
          actor_name: "投放同学",
          mutation: { field: "budget", mode: "increase_percent", value: "20" },
          source: { kind: "rule", rule_name: "自动加预算" },
          target_kinds: { adgroup: 3 },
        },
      ].filter(
        (row) => query.get("source") !== "rule" || row.source.kind === "rule",
      )
      // 首页之后由“加载更多”请求第二页。
      const page = query.get("cursor")
        ? [
            {
              ...rows[0],
              task_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
              created_at: "2026-10-13T08:00:00Z",
              mutation: { field: "roas", mode: "set", value: "1.5" },
            },
          ]
        : rows
      return handler.fulfill({
        json: {
          items: page,
          next_cursor: query.get("cursor") ? null : "next-page",
          total: rows.length + 1,
        },
      })
    }
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
  return { previewBodies, listQueries, renameSelectionBodies }
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
    // 面板先说明改哪一层，再给出“选中 → 将修改”的摘要和带名称的明细。
    await expect(page.getByText("只暂停所选的系列本身")).toBeVisible()
    await expect(
      page.getByText("已选 1 个系列 → 将修改 1 个对象（系列 1 个）"),
    ).toBeVisible()
    await expect(page.getByText("账户：adv-1")).toBeVisible()
    await expect(page.getByText("将暂停", { exact: true })).toBeVisible()
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
    // 暂停不涉及上级，不出现上级勾选，也不会带 include_parents。
    await expect(
      page.getByRole("checkbox", { name: "同时启用暂停中的上级" }),
    ).toHaveCount(0)
    expect(previewBodies[0]?.mutation?.include_parents).toEqual([])
    await expect(page.getByText("五分钟内有效")).toBeVisible()
    await page.getByRole("button", { name: "提交，修改 1 个对象" }).click()
    await expect(page).toHaveURL(
      new RegExp(`/tenants/${TENANT}/ad-management-tasks/${taskId}`),
    )
    await expect(
      page.getByRole("heading", { name: "操作记录详情" }),
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
    for (const name of [
      "启用",
      "暂停",
      "修改目标 ROAS",
      "修改预算",
      "批量改名",
      "按表格改名",
    ])
      await expect(page.getByRole("button", { name, exact: true })).toHaveCount(
        0,
      )
    await page.goto(`/tenants/${TENANT}/ad-management-tasks?bc_id=${BC_A}`)
    await expect(page.getByRole("heading", { name: "操作记录" })).toBeVisible()
    await expect(page.getByText("排队中")).toBeVisible()
    await page
      .getByRole("link", { name: `查看明细 ${taskId}`, exact: true })
      .click()
    await expect(
      page.getByText("当前为只读成员，隐藏取消、重试和恢复入口。"),
    ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "取消未发送项" }),
    ).toHaveCount(0)
    await expect(page.getByRole("button", { name: /刷新重试/ })).toHaveCount(0)
  })

  test("操作记录列表显示动作、对象、来源和结果，可筛选和加载更多", async ({
    page,
  }) => {
    const { listQueries } = await managementBoundary(page)
    await page.goto(`/tenants/${TENANT}/ad-management-tasks?bc_id=${BC_A}`)
    const manual = page.getByRole("row", { name: /暂停/ })
    await expect(manual).toContainText("系列 1")
    await expect(manual).toContainText("人工 · 投放同学")
    await expect(manual).toContainText("排队中")
    const byRule = page.getByRole("row", { name: /预算 提高 20%/ })
    await expect(byRule).toContainText("广告组 3")
    await expect(byRule).toContainText("自动规则 · 自动加预算")
    await expect(byRule).toContainText("部分完成")
    await expect(byRule).toContainText("成功 2")
    await expect(byRule).toContainText("失败 1")
    await expect(page.getByText("共 3 条")).toBeVisible()

    await page.getByRole("button", { name: "加载更多" }).click()
    await expect(
      page.getByRole("row", { name: /目标 ROAS 设为 1.5/ }),
    ).toBeVisible()
    await expect(page.getByRole("button", { name: "加载更多" })).toHaveCount(0)
    expect(listQueries[listQueries.length - 1]?.cursor).toBe("next-page")

    await page.getByRole("combobox", { name: "来源" }).click()
    await page.getByRole("option", { name: "自动规则" }).click()
    await expect(page).toHaveURL(/source=rule/)
    await expect(page.getByRole("row", { name: /暂停/ })).toHaveCount(0)
    await expect(page.getByRole("row", { name: /预算 提高 20%/ })).toBeVisible()
    expect(listQueries[listQueries.length - 1]).toMatchObject({
      bc_id: BC_A,
      source: "rule",
    })
    expect(listQueries[listQueries.length - 1]?.cursor).toBeUndefined()
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
    await expect(page.getByText("跳过无法修改的 1 项")).toBeVisible()
    await expect(
      page.getByText("当前预算为 0 或无效，无法在此基础上修改"),
    ).toBeVisible()
    await expect(page.getByRole("button", { name: /^提交/ })).toBeDisabled()
    await page.getByRole("checkbox", { name: "跳过无法修改的对象" }).click()
    await expect.poll(() => previewBodies.length).toBe(2)
    expect(previewBodies[1]?.mutation?.excluded_refs).toEqual([GROUP_REF])
    await expect(page.getByText("已跳过，不会提交")).toBeVisible()
    await expect(
      page.getByRole("button", { name: "提交，修改 1 个对象" }),
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

  test("启用时列出暂停中的上级，勾选后把这些上级一起提交", async ({ page }) => {
    const { previewBodies } = await managementBoundary(page, {
      blockedParent: true,
    })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await page
      .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" })
      .check()
    await page.getByRole("button", { name: "启用", exact: true }).click()
    await expect(page.getByText("有 1 个上级处于暂停状态")).toBeVisible()
    await expect(page.getByText("系列 · 暂停中的上级系列")).toBeVisible()
    await expect(
      page.getByRole("button", { name: "提交，修改 1 个对象" }),
    ).toBeEnabled()
    await page.getByRole("checkbox", { name: "同时启用暂停中的上级" }).click()
    await expect.poll(() => previewBodies.length).toBe(2)
    // 传给后端的是暂停中的上级本身，而不是用户选中的对象。
    expect(previewBodies[1]?.mutation?.include_parents).toEqual([PARENT_REF])
    // 上级变成待修改项后提示与勾选仍保留，用户可以随时取消。
    await expect(
      page.getByRole("checkbox", { name: "同时启用暂停中的上级" }),
    ).toBeChecked()
    await expect(
      page.getByRole("button", { name: "提交，修改 2 个对象" }),
    ).toBeEnabled()
  })

  test("预览被拒绝时显示服务器给出的原因", async ({ page }) => {
    const message =
      "同一系列的广告组目标 ROAS 必须一致；这些广告组原值不同，按百分比或加减调整会得到不同结果，请改用“设为”统一数值"
    await managementBoundary(page, {
      previewError: {
        status: 409,
        code: "management_roas_series_conflict",
        message,
      },
    })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await page
      .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" })
      .check()
    await page.getByRole("button", { name: "修改目标 ROAS" }).click()
    await expect(
      page.getByText(
        "目标 ROAS 只设置在广告组上，系列和广告没有单独的目标 ROAS。",
      ),
    ).toBeVisible()
    await page.getByRole("textbox", { name: "管理新值" }).fill("10")
    await expect(page.getByText(message)).toBeVisible()
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
    await expect(page.getByText("请输入大于 0 的数值")).toBeVisible()
    await expect(page.getByRole("status", { name: /正在生成/ })).toHaveCount(0)
    await page.getByRole("combobox", { name: "调整方式" }).click()
    await page.getByRole("option", { name: "增加百分比" }).click()
    // 空值或非法数值都不能发出预览请求，避免默认值误改真实广告。
    await page.getByRole("textbox", { name: "管理新值" }).fill("abc")
    await page.waitForTimeout(700)
    expect(previewBodies).toHaveLength(0)
    // 0 与科学计数法、十六进制等后端 Decimal 不认的写法同样不发预览。
    for (const invalid of ["0", "1e3", "0x10"]) {
      await page.getByRole("textbox", { name: "管理新值" }).fill(invalid)
      await expect(page.getByText("请输入大于 0 的数值")).toBeVisible()
      await page.waitForTimeout(700)
      expect(previewBodies).toHaveLength(0)
    }
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

  test("推广系列勾选后批量改名，前缀生成预览并提交", async ({ page }) => {
    const { previewBodies } = await managementBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await page
      .getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" })
      .check()
    await page.getByRole("button", { name: "批量改名" }).click()
    await expect(page.getByRole("heading", { name: "批量改名" })).toBeVisible()
    // 勾选入口默认查找替换页签；未点“生成预览”前不请求预览。
    await expect(page.getByRole("tab", { name: "查找替换" })).toHaveAttribute(
      "aria-selected",
      "true",
    )
    await page.getByRole("tab", { name: "前缀后缀" }).click()
    await expect(page.getByText("在名称最前面或最后面加上文字")).toBeVisible()
    await page.getByLabel("前缀", { exact: true }).fill("【停】")
    expect(previewBodies).toHaveLength(0)
    await page.getByRole("button", { name: "生成预览" }).click()
    await expect(page.getByText("报表中将归到 剧A")).toBeVisible()
    expect(previewBodies.at(-1)?.mutation).toMatchObject({
      field: "name",
      mode: "set",
      rename: { kind: "affix", prefix: "【停】" },
    })
    await expect(
      page.getByText("共 1 条：修改 1、跳过 0、异常 0"),
    ).toBeVisible()
    await expect(
      page.getByText("改名会同步修改 TikTok 后台的推广系列名称"),
    ).toBeVisible()
    // 生成预览后再改规则，必须重新生成预览才能提交。
    await page.getByLabel("后缀", { exact: true }).fill("-1015")
    await expect(
      page.getByText("改名规则已修改，请重新生成预览。"),
    ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "提交，修改 1 个对象" }),
    ).toBeDisabled()
    await page.getByLabel("后缀", { exact: true }).fill("")
    await page.getByRole("button", { name: "提交，修改 1 个对象" }).click()
    await expect(page).toHaveURL(
      new RegExp(`/tenants/${TENANT}/ad-management-tasks/${taskId}`),
    )
  })

  test("按表格改名：粘贴两列，找不到的 ID 标为异常", async ({ page }) => {
    const { previewBodies, renameSelectionBodies } =
      await managementBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列", exact: true }).click()
    await page.getByRole("button", { name: "按表格改名" }).click()
    await page
      .getByLabel("粘贴推广系列 ID 和新名称")
      .fill(
        "推广系列ID\t新名称\n1700000000000000001\t嘉书-剧A-1015\n1799999999999999999\tx",
      )
    await expect(page.getByText("已识别 2 行")).toBeVisible()
    await page.getByRole("button", { name: "生成预览" }).click()
    await expect(page.getByText("1799999999999999999")).toBeVisible()
    await expect(page.getByText("未找到该推广系列")).toBeVisible()
    expect(renameSelectionBodies[0]).toEqual({
      bc_id: BC_A,
      rows: [
        { campaign_id: "1700000000000000001", new_name: "嘉书-剧A-1015" },
        { campaign_id: "1799999999999999999", new_name: "x" },
      ],
    })
    await expect.poll(() => previewBodies.length).toBe(1)
    expect(previewBodies[0]).toMatchObject({
      selection_id: selectionId,
      mutation: {
        field: "name",
        rename: {
          kind: "explicit",
          names: {
            "1700000000000000001": "嘉书-剧A-1015",
            "1799999999999999999": "x",
          },
        },
      },
    })
    await expect(
      page.getByText("共 2 条：修改 1、跳过 0、异常 1"),
    ).toBeVisible()
    await expect(
      page.getByRole("button", { name: "仅提交可修改的行" }),
    ).toBeEnabled()
  })
})

test.describe("parsePastedRows", () => {
  test("忽略表头，支持 Tab 与逗号分隔", () => {
    expect(
      parsePastedRows(
        "推广系列ID\t新名称\n1700000000000000001\t嘉书-剧A\n\n1700000000000000002,网眼-剧B,第二季\r\n",
      ),
    ).toEqual({
      rows: [
        { campaignId: "1700000000000000001", newName: "嘉书-剧A" },
        { campaignId: "1700000000000000002", newName: "网眼-剧B,第二季" },
      ],
      errors: [],
    })
  })

  test("首行是数字 ID 时不当表头，缺少新名称的行报错", () => {
    expect(
      parsePastedRows("1700000000000000001,剧A\n1700000000000000002"),
    ).toEqual({
      rows: [{ campaignId: "1700000000000000001", newName: "剧A" }],
      errors: ["第 2 行缺少推广系列 ID 或新名称"],
    })
  })

  test("超过 500 行返回错误", () => {
    const text = Array.from(
      { length: 501 },
      (_, index) =>
        `17000000000000${String(index).padStart(5, "0")},名称${index}`,
    ).join("\n")
    expect(parsePastedRows(text).errors).toEqual([
      "单次最多 500 行，请分批粘贴",
    ])
  })

  test("格式异常的首行不当表头，交给服务器校验 ID", () => {
    expect(parsePastedRows("1.7E+18\t剧A\n１７００\t剧B")).toEqual({
      rows: [
        { campaignId: "1.7E+18", newName: "剧A" },
        { campaignId: "１７００", newName: "剧B" },
      ],
      errors: [],
    })
  })

  test("首列含空格或全角逗号时提示改用 Tab 或逗号分隔", () => {
    expect(
      parsePastedRows("1700000000000000001 剧A\n1700000000000000002，剧B")
        .errors,
    ).toEqual([
      "第 1 行未分出两列，请用 Tab 或逗号分隔推广系列 ID 和新名称",
      "第 2 行未分出两列，请用 Tab 或逗号分隔推广系列 ID 和新名称",
    ])
  })

  test("同一 ID 粘贴多次时以第一行的新名称为准", () => {
    expect(
      renameRuleFrom({
        kind: "explicit",
        find: "",
        replaceWith: "",
        prefix: "",
        suffix: "",
        text: "1700000000000000001\t剧A\n1700000000000000001\t剧B",
      }),
    ).toEqual({
      kind: "explicit",
      names: { "1700000000000000001": "剧A" },
    })
  })
})
