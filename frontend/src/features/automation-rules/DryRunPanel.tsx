import { Fragment, useState } from "react"
import type { CatalogPublic, DryRunPublic, HitPublic } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  LEVEL_LABELS,
  type Level,
  RANGE_LABELS,
  type RangeType,
} from "./ruleForm"
import { SkippedInputs } from "./SkippedInputs"

/** 指标键 "spend:TODAY" 显示为「今天消耗」；属性键直接取中文名。 */
export function metricTitle(key: string, catalog: CatalogPublic | undefined) {
  const [metric, range] = key.split(":")
  const name =
    catalog?.metrics.find((item) => item.key === metric)?.name_zh ?? metric
  return `${range ? RANGE_LABELS[range as RangeType] : ""}${name}`
}

export const OUTCOME_LABELS: Record<string, string> = {
  NOTIFIED: "已通知",
  PENDING_CONFIRMATION: "待确认",
  NOT_SELECTED: "未执行",
  NO_CHANGE: "无需变更",
  UNSUPPORTED: "不支持",
  SKIPPED_FREQUENCY: "频控跳过",
  SKIPPED_DAILY_LIMIT: "超出每日涨幅（历史）",
  SUBMITTED: "已提交",
  CONFLICT: "规则冲突",
  FAILED: "失败",
}

export function HitTable({
  hits,
  catalog,
  showOutcome = false,
}: {
  hits: HitPublic[]
  catalog: CatalogPublic | undefined
  /** 运行明细显示每条命中的处理结果；试运行没有执行，不显示。 */
  showOutcome?: boolean
}) {
  const [open, setOpen] = useState<string | null>(null)
  const columns = showOutcome ? 6 : 5
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>对象</TableHead>
          <TableHead>剧</TableHead>
          <TableHead>账户</TableHead>
          <TableHead>指标</TableHead>
          <TableHead className="text-right">涉及对象</TableHead>
          {showOutcome && <TableHead>处理结果</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {hits.map((hit) => (
          <Fragment key={hit.subject_key}>
            <TableRow>
              <TableCell
                className="max-w-56 truncate"
                title={hit.subject_label}
              >
                {hit.subject_label}
              </TableCell>
              <TableCell className="max-w-40 truncate">
                {/* 剧合计时对象本身就是剧，剧列不重复显示 */}
                {hit.subject_key.startsWith("drama:")
                  ? "—"
                  : (hit.drama_name ?? "—")}
              </TableCell>
              <TableCell>{hit.advertiser_id ?? "—"}</TableCell>
              <TableCell className="whitespace-normal">
                {Object.entries(hit.metrics)
                  .map(
                    ([key, value]) =>
                      `${metricTitle(key, catalog)} ${value || "—"}`,
                  )
                  .join("；")}
              </TableCell>
              <TableCell className="text-right">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() =>
                    setOpen(open === hit.subject_key ? null : hit.subject_key)
                  }
                >
                  {hit.target_count} 个
                </Button>
              </TableCell>
              {showOutcome && (
                <TableCell>
                  {OUTCOME_LABELS[hit.outcome ?? ""] ?? hit.outcome ?? "—"}
                </TableCell>
              )}
            </TableRow>
            {open === hit.subject_key && (
              <TableRow>
                <TableCell
                  colSpan={columns}
                  className="bg-muted/40 whitespace-normal"
                >
                  <ul className="flex flex-col gap-1 text-sm">
                    {hit.targets.map((target) => (
                      <li key={String(target.key)}>
                        {LEVEL_LABELS[target.kind as Level] ??
                          String(target.kind)}{" "}
                        · {String(target.name)} · 账户{" "}
                        {String(target.advertiser_id)}
                      </li>
                    ))}
                    {hit.targets.length < hit.target_count && (
                      <li className="text-muted-foreground">
                        仅显示前 {hit.targets.length} 个
                      </li>
                    )}
                  </ul>
                </TableCell>
              </TableRow>
            )}
          </Fragment>
        ))}
      </TableBody>
    </Table>
  )
}

export function DryRunPanel({
  result,
  catalog,
}: {
  result: DryRunPublic
  catalog: CatalogPublic | undefined
}) {
  return (
    <section aria-label="试运行结果" className="flex flex-col gap-3">
      <p className="text-sm font-medium">
        判断 {result.checked_count} 个对象，命中 {result.hit_count} 个
      </p>
      <SkippedInputs items={result.skipped_accounts} />
      {result.skipped_count > 0 && (
        <p className="text-sm text-muted-foreground">
          {result.skipped_count} 个对象因命名或素材数据不完整，未参与判断
        </p>
      )}
      {result.hits.length > 0 && (
        <HitTable hits={result.hits} catalog={catalog} />
      )}
    </section>
  )
}
