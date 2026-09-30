import type { Page } from "@playwright/test"

export const TENANT = "11111111-1111-4111-8111-111111111111"
export const BC_A = "bc-a"
export const BC_B = "bc-b"
export const ADVERTISER = "adv-1"
const token = "ads-workspace-test-token"

export type AdsBoundaryOptions = { delayedA?: boolean }

export async function adsBoundary(
  page: Page,
  options: AdsBoundaryOptions = {},
) {
  await page.addInitScript(
    (value) => localStorage.setItem("access_token", value),
    token,
  )
  await page.route("**/api/**", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    const headers = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "*",
      "Access-Control-Allow-Methods": "*",
    }
    if (request.method() === "OPTIONS")
      return route.fulfill({ status: 204, headers })
    if (path === "/api/users/me")
      return route.fulfill({
        headers,
        json: {
          id: "44444444-4444-4444-8444-444444444444",
          username: "ads-test",
          full_name: "报表测试",
          is_active: true,
          is_superuser: false,
        },
      })
    if (path === "/api/me/tenants")
      return route.fulfill({
        headers,
        json: {
          items: [
            {
              id: TENANT,
              name: "报表租户",
              active: true,
              role: "tenant_admin",
              default_bc_id: BC_A,
            },
          ],
          next_cursor: null,
          total: 1,
        },
      })
    if (path.endsWith(`/tenants/${TENANT}/bcs`)) {
      return route.fulfill({
        headers,
        json: {
          items: [
            { bc_id: BC_A, name: "甲 BC" },
            { bc_id: BC_B, name: "乙 BC" },
          ],
          next_cursor: null,
          total: 2,
        },
      })
    }
    if (path.endsWith(`/tenants/${TENANT}/ads`)) {
      const bc = url.searchParams.get("bc_id")
      if (bc === BC_A && options.delayedA)
        await new Promise((resolve) => setTimeout(resolve, 700))
      const name = bc === BC_B ? "乙-其他剧-测试" : "嘉书-总裁归来-测试"
      const dimension = url.searchParams.get("dimension")
      const display =
        dimension === "drama"
          ? { name: "总裁归来", drama_name: "总裁归来", provider: "嘉书" }
          : {
              name,
              campaign_name: name,
              drama_name: "总裁归来",
              advertiser_id: ADVERTISER,
              status: "ENABLE",
              target_roas: "1.50",
            }
      return route.fulfill({
        headers,
        json: {
          snapshot: {
            snapshot_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            expires_at: "2099-01-01T00:00:00Z",
            filters: {
              dimension: dimension ?? "campaign",
              start_date: "2026-10-01",
              end_date: "2026-10-01",
            },
            publication_versions: {},
          },
          items: [
            {
              row_key: `${bc}-campaign-1`,
              display,
              refs: [
                {
                  tenant_id: TENANT,
                  advertiser_id: ADVERTISER,
                  kind: "campaign",
                  remote_id: "campaign-1",
                },
              ],
              material_uses: [],
              metric_buckets: [
                {
                  currency: "USD",
                  timezone: "UTC",
                  attribution: "IAA",
                  values: {
                    spend: "100",
                    native_growth_ad_revenue_value_d0: "120",
                    d0_roas: "1.2",
                  },
                  availability: {
                    spend: "AVAILABLE",
                    native_growth_ad_revenue_value_d0: "AVAILABLE",
                    d0_roas: "AVAILABLE",
                  },
                },
              ],
              capabilities: { can_manage: true },
              coverage: {
                status: "COMPLETE",
                latest_sync_at: "2026-10-01T00:00:00Z",
              },
            },
          ],
          total: 1,
          summary: {
            d0_roas: "1.2",
            spend: "100",
            native_growth_ad_revenue_value_d0: "120",
          },
          coverage: {
            status: "COMPLETE",
            latest_sync_at: "2026-10-01T00:00:00Z",
          },
          next_cursor: null,
        },
      })
    }
    if (path.endsWith("/reports/trend"))
      return route.fulfill({
        headers,
        json: {
          points: [],
          coverage: { status: "COMPLETE" },
          actual_interval_minutes: 30,
        },
      })
    if (path.endsWith("/report-views"))
      return route.fulfill({ headers, json: [] })
    return route.fulfill({
      headers,
      status: 404,
      json: { detail: "Unexpected ads test API request" },
    })
  })
}
