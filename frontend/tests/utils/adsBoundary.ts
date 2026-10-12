import type { Page } from "@playwright/test"
import METRIC_CATALOG from "../fixtures/metricCatalog.json" with {
  type: "json",
}

export const TENANT = "11111111-1111-4111-8111-111111111111"
// BC 使用路由接受的十进制字符串，覆盖大于 JS 安全整数的真实 ID 格式。
export const BC_A = "90071992547409931111"
export const BC_B = "90071992547409932222"
export const ADVERTISER = "adv-1"
const token = "ads-workspace-test-token"

export type AdsBoundaryOptions = {
  lastSuccess?: string
  slowed?: boolean
  delayedA?: boolean
  paged?: boolean
  mixedBuckets?: boolean
  refreshStatus?: "QUEUED" | "SYNC_DISABLED" | "API_BINDING_REQUIRED"
  incomplete?: boolean
  reconciliation?: boolean
  balance?: boolean
  /** 消耗列标为 PARTIAL，但行覆盖仍为 COMPLETE（成员账户平台返回 NULL 的情形）。 */
  partialSpend?: boolean
}

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
    if (path.endsWith(`/tenants/${TENANT}/ads/metric-catalog`))
      return route.fulfill({ headers, json: METRIC_CATALOG })
    if (path.endsWith(`/tenants/${TENANT}/ads`)) {
      const bc = url.searchParams.get("bc_id")
      if (bc === BC_A && options.delayedA)
        await new Promise((resolve) => setTimeout(resolve, 700))
      const secondPage = Boolean(url.searchParams.get("cursor"))
      const name = secondPage
        ? "嘉书-第二页-测试"
        : bc === BC_B
          ? "乙-其他剧-测试"
          : "嘉书-总裁归来-测试"
      const dimension = url.searchParams.get("dimension")
      const display =
        dimension === "drama"
          ? { name: "总裁归来", drama_name: "总裁归来", provider: "嘉书" }
          : dimension === "account"
            ? {
                name: bc === BC_B ? "乙账户" : "甲账户",
                account_name: bc === BC_B ? "乙账户" : "甲账户",
                account_id: ADVERTISER,
                advertiser_id: ADVERTISER,
                remote_id: ADVERTISER,
                account_status: "STATUS_ENABLE",
              }
            : dimension === "adgroup"
              ? {
                  name,
                  account_name: "甲账户",
                  account_id: ADVERTISER,
                  campaign_name: "嘉书-总裁归来-测试",
                  campaign_id: "campaign-1",
                  adgroup_name: name,
                  adgroup_id: "adgroup-1",
                  advertiser_id: ADVERTISER,
                  remote_id: "adgroup-1",
                  operation_status: "ENABLE",
                  secondary_status: "ADGROUP_STATUS_DELIVERY_OK",
                }
              : dimension === "ad"
                ? {
                    name,
                    account_name: "甲账户",
                    account_id: ADVERTISER,
                    campaign_name: "嘉书-总裁归来-测试",
                    campaign_id: "campaign-1",
                    adgroup_name: "嘉书-总裁归来-测试",
                    adgroup_id: "adgroup-1",
                    ad_name: name,
                    ad_id: "ad-1",
                    advertiser_id: ADVERTISER,
                    remote_id: "ad-1",
                    operation_status: "ENABLE",
                    secondary_status: "AD_STATUS_DELIVERY_OK",
                  }
                : dimension === "material"
                  ? {
                      name: "素材-测试",
                      material_name: "素材-测试",
                      main_material_id: "material-1",
                      account_id: ADVERTISER,
                      account_name: "甲账户",
                      advertiser_id: ADVERTISER,
                    }
                  : {
                      name,
                      campaign_name: name,
                      account_name: "甲账户",
                      account_id: ADVERTISER,
                      campaign_id: "campaign-1",
                      drama_name: "总裁归来",
                      advertiser_id: ADVERTISER,
                      remote_id: "campaign-1",
                      operation_status: "DISABLE",
                      secondary_status: "CAMPAIGN_STATUS_DISABLE",
                      campaign_budget: "50",
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
              display: {
                ...display,
                ...(options.balance
                  ? {
                      balance: "12.50",
                      balance_currency: "CNY",
                      balance_scope: "PORTFOLIO",
                      balance_updated_at: "2026-10-12T00:00:00Z",
                    }
                  : {}),
                ...(options.reconciliation
                  ? { reconciliation: "DIFF", account_report_spend: "101" }
                  : {}),
              },
              // 账户行在后端裁剪了列表引用，只保留 display.account_id。
              refs:
                dimension === "account"
                  ? []
                  : [
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
                    revenue_d0: "120",
                    revenue_total: "150",
                    ad_impression_events: "50",
                    impressions: "1000",
                    clicks: "20",
                    roas_total: "1.5",
                    roas_d0: "1.2",
                    roas_d3: dimension === "material" ? null : "1.3",
                    roas_d7: "1.4",
                    roas_d14: "1.5",
                    roas_d30: dimension === "material" ? null : "1.6",
                    cost_per_ad_impression_event: "2",
                    ctr: "0.02",
                  },
                  availability: {
                    spend: options.partialSpend ? "PARTIAL" : "AVAILABLE",
                    revenue_d0: "AVAILABLE",
                    revenue_total: "AVAILABLE",
                    ad_impression_events: "AVAILABLE",
                    impressions: "AVAILABLE",
                    clicks: "AVAILABLE",
                    roas_total: "AVAILABLE",
                    roas_d0: "AVAILABLE",
                    roas_d3:
                      dimension === "material" ? "UNSUPPORTED" : "AVAILABLE",
                    roas_d7: "AVAILABLE",
                    roas_d14: "AVAILABLE",
                    roas_d30:
                      dimension === "material" ? "UNSUPPORTED" : "AVAILABLE",
                    cost_per_ad_impression_event: "AVAILABLE",
                    ctr: "AVAILABLE",
                  },
                },
                ...(options.mixedBuckets
                  ? [
                      {
                        currency: "CNY",
                        timezone: "Asia/Shanghai",
                        attribution: "IAA",
                        values: {
                          spend: "100",
                          revenue_d0: "120",
                          revenue_total: "150",
                          ad_impression_events: "50",
                          impressions: "1000",
                          clicks: "20",
                          roas_total: "1.5",
                          roas_d0: "1.2",
                          roas_d3: dimension === "material" ? null : "1.3",
                          roas_d7: "1.4",
                          roas_d14: "1.5",
                          roas_d30: dimension === "material" ? null : "1.6",
                          cost_per_ad_impression_event: "2",
                          ctr: "0.02",
                        },
                        availability: {
                          spend: "AVAILABLE",
                          revenue_d0: "AVAILABLE",
                          revenue_total: "AVAILABLE",
                          ad_impression_events: "AVAILABLE",
                          impressions: "AVAILABLE",
                          clicks: "AVAILABLE",
                          roas_total: "AVAILABLE",
                          roas_d0: "AVAILABLE",
                          roas_d3:
                            dimension === "material"
                              ? "UNSUPPORTED"
                              : "AVAILABLE",
                          roas_d7: "AVAILABLE",
                          roas_d14: "AVAILABLE",
                          roas_d30:
                            dimension === "material"
                              ? "UNSUPPORTED"
                              : "AVAILABLE",
                          cost_per_ad_impression_event: "AVAILABLE",
                          ctr: "AVAILABLE",
                        },
                      },
                    ]
                  : []),
              ],
              capabilities: { can_manage: true },
              coverage: {
                status: "COMPLETE",
                updated_at: "2026-10-01T00:00:00Z",
              },
            },
          ],
          total: 1,
          summary: {
            row_count: 1,
            buckets: [
              {
                currency: "USD",
                timezone: "UTC",
                attribution: "IAA",
                values: {
                  spend: "100",
                  revenue_d0: "120",
                  revenue_total: "150",
                  ad_impression_events: "50",
                  impressions: "1000",
                  clicks: "20",
                  roas_total: "1.5",
                  roas_d0: "1.2",
                  roas_d3: dimension === "material" ? null : "1.3",
                  roas_d7: "1.4",
                  roas_d14: "1.5",
                  roas_d30: dimension === "material" ? null : "1.6",
                  cost_per_ad_impression_event: "2",
                  ctr: "0.02",
                },
                availability: {
                  spend: "AVAILABLE",
                  revenue_d0: "AVAILABLE",
                  revenue_total: "AVAILABLE",
                  ad_impression_events: "AVAILABLE",
                  impressions: "AVAILABLE",
                  clicks: "AVAILABLE",
                  roas_total: "AVAILABLE",
                  roas_d0: "AVAILABLE",
                  roas_d3:
                    dimension === "material" ? "UNSUPPORTED" : "AVAILABLE",
                  roas_d7: "AVAILABLE",
                  roas_d14: "AVAILABLE",
                  roas_d30:
                    dimension === "material" ? "UNSUPPORTED" : "AVAILABLE",
                  cost_per_ad_impression_event: "AVAILABLE",
                  ctr: "AVAILABLE",
                },
              },
              ...(options.mixedBuckets
                ? [
                    {
                      currency: "CNY",
                      timezone: "Asia/Shanghai",
                      attribution: "IAA",
                      values: {
                        spend: "100",
                        revenue_d0: "120",
                        revenue_total: "150",
                        ad_impression_events: "50",
                        impressions: "1000",
                        clicks: "20",
                        roas_total: "1.5",
                        roas_d0: "1.2",
                        roas_d3: dimension === "material" ? null : "1.3",
                        roas_d7: "1.4",
                        roas_d14: "1.5",
                        roas_d30: dimension === "material" ? null : "1.6",
                        cost_per_ad_impression_event: "2",
                        ctr: "0.02",
                      },
                      availability: {
                        spend: "AVAILABLE",
                        revenue_d0: "AVAILABLE",
                        revenue_total: "AVAILABLE",
                        ad_impression_events: "AVAILABLE",
                        impressions: "AVAILABLE",
                        clicks: "AVAILABLE",
                        roas_total: "AVAILABLE",
                        roas_d0: "AVAILABLE",
                        roas_d3:
                          dimension === "material"
                            ? "UNSUPPORTED"
                            : "AVAILABLE",
                        roas_d7: "AVAILABLE",
                        roas_d14: "AVAILABLE",
                        roas_d30:
                          dimension === "material"
                            ? "UNSUPPORTED"
                            : "AVAILABLE",
                        cost_per_ad_impression_event: "AVAILABLE",
                        ctr: "AVAILABLE",
                      },
                    },
                  ]
                : []),
            ],
          },
          coverage: {
            status: "COMPLETE",
            updated_at: "2026-10-01T00:00:00Z",
          },
          next_cursor: options.paged && !secondPage ? "cursor-1" : null,
        },
      })
    }
    if (path.endsWith(`/tenants/${TENANT}/ads/campaign/campaign-1`))
      return route.fulfill({
        headers,
        json: {
          ref: {
            tenant_id: TENANT,
            advertiser_id: ADVERTISER,
            kind: "campaign",
            remote_id: "campaign-1",
          },
          name: "嘉书-总裁归来-测试",
          ad_type: "VIDEO",
          configuration: {},
          statuses: { operation: "PAUSED", review: "APPROVED" },
          materials: [],
          operation_history: [],
          observed_at: "2026-10-01T00:00:00Z",
        },
      })
    if (path.endsWith("/ad-sync-status"))
      return route.fulfill({
        headers,
        json: {
          accounts: [
            {
              advertiser_id: ADVERTISER,
              timezone: "Asia/Hong_Kong",
              last_success_at: options.lastSuccess ?? "2026-10-05T05:00:00Z",
              incomplete_dates: options.incomplete ? ["2026-10-04"] : [],
              failed_dates: [],
            },
          ],
          publication_version: options.lastSuccess ?? "version-1",
          plan: {
            enabled: true,
            disabled_reason: null,
            next_hot_at: null,
            slowed: options.slowed ?? false,
          },
        },
      })
    if (path.endsWith("/ad-sync-refresh"))
      return route.fulfill({
        headers,
        json: {
          queued: 3,
          accounts: 1,
          status: options.refreshStatus ?? "QUEUED",
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
