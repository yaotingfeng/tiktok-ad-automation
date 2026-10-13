import { Fragment, useState } from "react"
import type { CatalogPublic, DryRunPublic, HitPublic } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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

/** 指标键 "spend:TODAY" 显示为「今天消耗」；属性键直接取中文名。 */
export function metricTitle(key: string, catalog: CatalogPublic | undefined) {
  const [metric, range] = key.split(":")
  const name =
    catalog?.metrics.find((item) => item.key === metric)?.name_zh ?? metric
  return `${range ? RANGE_LABELS[range as RangeType] : ""}${name}`
}

export function HitTable({
  hits,
  catalog,
}: {
  hits: HitPublic[]
  catalog: CatalogPublic | undefined
}) {
  const [open, setOpen] = useState<string | null>(null)
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>对象</TableHead>
          <TableHead>剧</TableHead>
          <TableHead>账户</TableHead>
          <TableHead>指标</TableHead>
          <TableHead className="text-right">涉及对象</TableHead>
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
            </TableRow>
            {open === hit.subject_key && (
              <TableRow>
                <TableCell
                  colSpan={5}
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
      {result.skipped_accounts.length > 0 && (
        <Alert>
          <AlertTitle>
            数据未更新，已跳过 {result.skipped_accounts.length} 个账户
          </AlertTitle>
          <AlertDescription className="break-all">
            {result.skipped_accounts
              .map((item) => item.advertiser_id)
              .join("、")}
          </AlertDescription>
        </Alert>
      )}
      {result.skipped_count > 0 && (
        <p className="text-sm text-muted-foreground">
          {result.skipped_count} 个对象命名不规范，未参与剧合计
        </p>
      )}
      {result.hits.length > 0 && (
        <HitTable hits={result.hits} catalog={catalog} />
      )}
    </section>
  )
}
