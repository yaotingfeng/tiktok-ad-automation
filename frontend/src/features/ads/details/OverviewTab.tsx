import type { ReactNode } from "react"
import type { AdDetailPublic, ReportRow } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { type ColumnDef, formatMetric } from "../metricCatalog"
import {
  type AdsDimension,
  metricOf,
  type ParentLink,
  parentPath,
  rowAdvertiserId,
  rowObjectId,
} from "../search"
import { statusLabel } from "../statusLabel"

const PARENT_KIND: Record<string, string> = {
  campaign: "系列",
  adgroup: "广告组",
  ad: "广告",
  account: "账户",
}
const STATUS_VALUES: Record<string, string> = {
  ENABLE: "已开启",
  DISABLE: "已暂停",
  DELETE: "已删除",
}
const REASON_PREFIX: Record<string, string> = {
  operation_status: "上级操作状态",
  review_status: "上级审核状态",
  delivery_status: "上级投放状态",
}

const REVIEW_VALUES: Record<string, string> = {
  APPROVED: "已通过",
  REVIEW_PASS: "已通过",
  IN_REVIEW: "审核中",
  AUDIT: "审核中",
  REJECTED: "未通过",
  AUDIT_DENY: "未通过",
}

// 状态枚举翻成中文显示，原值放在悬停说明里；未登记的值原样显示。
function StatusValue({
  kind,
  value,
}: {
  kind: "operation" | "review" | "delivery"
  value: string | null | undefined
}) {
  if (!value) return <>—</>
  let label = value
  if (kind === "operation") label = STATUS_VALUES[value] ?? value
  else if (kind === "review" && REVIEW_VALUES[value])
    label = REVIEW_VALUES[value]
  else {
    const parsed = statusLabel({ secondary_status: value })
    if (parsed.label !== "未知") label = parsed.label
  }
  return <span title={value}>{label}</span>
}

// 后端把父级限制编码为 "operation_status:DISABLE" 一类字符串；翻成中文，未知值原样显示。
export function parentReasonText(reason: string): string {
  if (reason === "parent_not_synced") return "上级尚未同步"
  const [prefix, ...rest] = reason.split(":")
  const label = REASON_PREFIX[prefix]
  if (!label || !rest.length) return reason
  const value = rest.join(":")
  return `${label}：${STATUS_VALUES[value] ?? value}`
}

// 命名规则为 版权方-剧名-备注；后端已解析时优先用解析结果。
function namingParts(row: ReportRow, name: string) {
  const display = row.display ?? {}
  const parts = name.split("-")
  return {
    provider: display.provider ?? (parts.length >= 3 ? parts[0] : null),
    drama: display.drama_name ?? (parts.length >= 3 ? parts[1] : null),
    remark: parts.length >= 3 ? parts.slice(2).join("-") : null,
    invalid:
      display.naming_status === "INVALID" ||
      (!display.naming_status && parts.length < 3),
  }
}

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="break-words text-sm">{value ?? "—"}</dd>
    </div>
  )
}

function metricText(row: ReportRow, column: ColumnDef): string {
  const buckets = row.metric_buckets ?? []
  if (!buckets.length) return "—"
  // 多币种逐桶显示，不跨币种相加。
  return buckets
    .map((bucket, index) => {
      const text = formatMetric(
        metricOf(row, column.key, index).value,
        column.unit,
      )
      return buckets.length > 1 ? `${bucket.currency} ${text}` : text
    })
    .join(" / ")
}

export function OverviewTab({
  row,
  dimension,
  name,
  detail,
  loading,
  columns,
  onOpenParent,
}: {
  row: ReportRow
  dimension: AdsDimension
  name: string
  detail: AdDetailPublic | undefined
  loading: boolean
  columns: ColumnDef[]
  onOpenParent: (link: ParentLink, advertiserId: string) => void
}) {
  const display = row.display ?? {}
  const advertiserId = rowAdvertiserId(row)
  const path = parentPath(row, dimension)
  const naming = namingParts(row, name)
  const status = statusLabel(display)
  const firstBucket = row.metric_buckets?.[0]
  const relatedCampaigns = (row.refs ?? []).filter(
    (ref) => ref.kind === "campaign",
  )
  return (
    <div className="flex flex-col gap-5 text-sm">
      <dl className="grid grid-cols-2 gap-3 md:grid-cols-3">
        <Field label="ID" value={rowObjectId(row, dimension) ?? "—"} />
        {path.length > 0 && (
          <div className="col-span-2 flex flex-col gap-0.5">
            <dt className="text-xs text-muted-foreground">层级路径</dt>
            <dd className="flex flex-wrap items-center gap-1">
              {path.map((link, index) => (
                <span
                  key={link.kind}
                  className="inline-flex items-center gap-1"
                >
                  {index > 0 && <span aria-hidden="true">›</span>}
                  <span className="text-xs text-muted-foreground">
                    {PARENT_KIND[link.kind]}
                  </span>
                  <Button
                    type="button"
                    variant="link"
                    size="sm"
                    className="h-auto p-0"
                    title={`${PARENT_KIND[link.kind]} ${link.id}`}
                    onClick={() => onOpenParent(link, advertiserId)}
                  >
                    {link.name}
                  </Button>
                </span>
              ))}
            </dd>
          </div>
        )}
      </dl>

      {dimension === "account" ? (
        <section className="flex flex-col gap-2">
          <h3 className="font-medium">账户信息</h3>
          <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Field
              label="账户状态"
              value={<Badge variant={status.tone}>{status.label}</Badge>}
            />
            <Field label="余额" value={display.balance ?? "—"} />
            <Field
              label="币种"
              value={display.balance_currency ?? firstBucket?.currency ?? "—"}
            />
            <Field
              label="时区"
              value={display.timezone ?? firstBucket?.timezone ?? "—"}
            />
          </dl>
        </section>
      ) : (
        <section className="flex flex-col gap-2">
          <h3 className="font-medium">命名解析</h3>
          <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
            {/* 版权方即系列名第一段；剧跨多个写法合并时后端会全部列出。 */}
            <Field label="版权方" value={naming.provider ?? "—"} />
            <Field label="剧名" value={naming.drama ?? "—"} />
            {dimension !== "drama" && (
              <Field label="备注" value={naming.remark ?? "—"} />
            )}
          </dl>
          {dimension !== "drama" && naming.invalid && (
            <p className="text-xs text-amber-600">
              命名不符合 版权方-剧名-备注 规则
            </p>
          )}
        </section>
      )}

      {dimension !== "account" && dimension !== "drama" && (
        <section className="flex flex-col gap-2">
          <h3 className="font-medium">状态</h3>
          {loading ? (
            <Skeleton className="h-10 w-full" />
          ) : (
            <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Field
                label="主状态"
                value={<Badge variant={status.tone}>{status.label}</Badge>}
              />
              <Field
                label="操作状态"
                value={
                  <StatusValue
                    kind="operation"
                    value={
                      detail?.statuses?.operation ?? display.operation_status
                    }
                  />
                }
              />
              <Field
                label="审核状态"
                value={
                  <StatusValue
                    kind="review"
                    value={detail?.statuses?.review ?? display.review_status}
                  />
                }
              />
              <Field
                label="投放状态"
                value={
                  <StatusValue
                    kind="delivery"
                    value={
                      detail?.statuses?.delivery ?? display.secondary_status
                    }
                  />
                }
              />
            </dl>
          )}
          {(detail?.parent?.reasons?.length ?? 0) > 0 && (
            <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-xs dark:bg-amber-950/30">
              <p className="font-medium">上级限制实际投放</p>
              <ul className="mt-1 list-disc pl-4">
                {detail?.parent?.reasons?.map((reason) => (
                  <li key={reason}>{parentReasonText(reason)}</li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      {dimension === "drama" && (
        <section className="flex flex-col gap-2">
          <h3 className="font-medium">关联系列</h3>
          {relatedCampaigns.length ? (
            <ul className="flex flex-col gap-1">
              {relatedCampaigns.map((ref) => (
                <li
                  key={`${ref.advertiser_id}:${ref.remote_id}`}
                  className="flex items-center gap-2"
                >
                  <Button
                    type="button"
                    variant="link"
                    size="sm"
                    className="h-auto p-0"
                    onClick={() =>
                      onOpenParent(
                        {
                          kind: "campaign",
                          id: ref.remote_id,
                          name: ref.remote_id,
                        },
                        ref.advertiser_id,
                      )
                    }
                  >
                    {ref.remote_id}
                  </Button>
                  <span className="text-xs text-muted-foreground">
                    账户 {ref.advertiser_id}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted-foreground">暂无关联系列</p>
          )}
        </section>
      )}

      <section className="flex flex-col gap-2">
        <h3 className="font-medium">所选日期核心指标</h3>
        <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {columns.slice(0, 8).map((column) => (
            <div key={column.key} className="rounded-md border p-3">
              <dt
                className="truncate text-xs text-muted-foreground"
                title={column.fullName}
              >
                {column.label}
              </dt>
              <dd className="mt-1 text-base font-medium">
                {metricText(row, column)}
              </dd>
            </div>
          ))}
        </dl>
      </section>
    </div>
  )
}
