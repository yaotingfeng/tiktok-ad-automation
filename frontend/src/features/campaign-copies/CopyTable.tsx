import { Link } from "@tanstack/react-router"
import { Fragment, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { TargetingSummary } from "@/features/targeting/TargetingForm"
import type { CopyConfiguration, CopyDraftPublic, CopyRow } from "./api"
import type { EditorKind } from "./CopyEditors"

import { copyErrorLabel } from "./presentation"

export function CopyTable({
  rows,
  editable,
  selected,
  setSelected,
  change,
  open,
}: {
  rows: CopyRow[]
  editable: boolean
  selected: string[]
  setSelected: (ids: string[]) => void
  change: (ids: string[], patch: Partial<CopyConfiguration>) => void
  open: (kind: EditorKind, ids: string[]) => void
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>复制队列 · {rows.length} 条来源系列</CardTitle>
        <CardDescription>
          每行分别编辑；勾选仅用于批量填值，队列内所有来源参与复制。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-muted-foreground">
            已勾选 {selected.length} 条
          </span>
          {(
            [
              ["accounts", "选择账户"],
              ["target_roas", "设置 ROAS"],
              ["budget", "设置预算"],
              ["copies", "复制份数"],
              ["targeting", "修改定向"],
            ] as const
          ).map(([kind, label]) => (
            <Button
              key={kind}
              size="sm"
              variant="outline"
              disabled={
                !editable ||
                !selected.length ||
                (kind === "target_roas" &&
                  rows.some(
                    (row) =>
                      selected.includes(row.row_id) &&
                      row.source.bid_strategy !== "TARGET_ROAS",
                  ))
              }
              onClick={() => open(kind, selected)}
            >
              {label}
            </Button>
          ))}
        </div>
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>
                  <Checkbox
                    aria-label="勾选全部方案"
                    disabled={!editable}
                    checked={
                      selected.length === rows.length
                        ? true
                        : selected.length
                          ? "indeterminate"
                          : false
                    }
                    onCheckedChange={(checked) =>
                      setSelected(checked ? rows.map((row) => row.row_id) : [])
                    }
                  />
                </TableHead>
                <TableHead>源系列 / 投放链路</TableHead>
                <TableHead>目标账户</TableHead>
                <TableHead>每账户份数</TableHead>
                <TableHead>ROAS</TableHead>
                <TableHead>日预算</TableHead>
                <TableHead>受众定向</TableHead>
                <TableHead>素材</TableHead>
                <TableHead>更多设置</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <CopyTableRow
                  key={row.row_id}
                  row={row}
                  editable={editable}
                  selected={selected.includes(row.row_id)}
                  onSelect={(checked) =>
                    setSelected(
                      checked
                        ? [...selected, row.row_id]
                        : selected.filter((id) => id !== row.row_id),
                    )
                  }
                  change={(patch) => change([row.row_id], patch)}
                  open={(kind) => open(kind, [row.row_id])}
                />
              ))}
            </TableBody>
          </Table>
        </div>
        <p className="text-sm text-muted-foreground">
          当前仅支持同币种账户复制；预览核验目标权限、素材和小程序。文案沿用每条源广告的实际内容。
        </p>
      </CardContent>
    </Card>
  )
}
function CopyTableRow({
  row,
  editable,
  selected,
  onSelect,
  change,
  open,
}: {
  row: CopyRow
  editable: boolean
  selected: boolean
  onSelect: (checked: boolean) => void
  change: (patch: Partial<CopyConfiguration>) => void
  open: (kind: EditorKind) => void
}) {
  const { source, configuration: config } = row
  return (
    <TableRow data-testid={`copy-row-${row.row_id}`}>
      <TableCell>
        <Checkbox
          aria-label={`勾选 ${source.name}`}
          disabled={!editable}
          checked={selected}
          onCheckedChange={(checked) => onSelect(checked === true)}
        />
      </TableCell>
      <TableCell className="min-w-56 max-w-80 whitespace-normal">
        <p className="font-medium">{source.name}</p>
        <p className="text-xs text-muted-foreground">
          {source.advertiser_id} · {source.campaign_id}
        </p>
        <p className="text-xs text-muted-foreground">
          {[
            source.provider_name,
            source.application_name,
            source.minis_id ? `小程序 ${source.minis_id}` : null,
          ]
            .filter(Boolean)
            .join(" · ") || "投放链路与实际文案沿用来源"}
        </p>
        {source.blockers.map((blocker) => (
          <p key={blocker} role="alert" className="text-xs text-destructive">
            {blocker}
          </p>
        ))}
      </TableCell>
      <TableCell>
        <Button
          variant="outline"
          size="sm"
          disabled={!editable}
          onClick={() => open("accounts")}
        >
          {config.target_advertiser_ids.length
            ? `${config.target_advertiser_ids.length} 个账户`
            : "选择账户"}
        </Button>
      </TableCell>
      <TableCell>
        <Input
          className="w-20"
          aria-label={`${source.name} 每账户份数`}
          type="number"
          min={1}
          max={20}
          step={1}
          value={config.copies || ""}
          disabled={!editable}
          onChange={(event) => change({ copies: Number(event.target.value) })}
        />
      </TableCell>
      <TableCell>
        {source.bid_strategy === "TARGET_ROAS" ? (
          <Input
            className="w-24"
            aria-label={`${source.name} 目标 ROAS`}
            type="number"
            placeholder="多个值"
            min={0.01}
            step="any"
            value={config.target_roas ?? source.target_roas ?? ""}
            disabled={!editable}
            onChange={(event) =>
              change({ target_roas: event.target.value || null })
            }
          />
        ) : (
          <Badge variant="secondary">最高价值</Badge>
        )}
      </TableCell>
      <TableCell>
        <Input
          className="w-28"
          aria-label={`${source.name} 日预算`}
          type="number"
          placeholder="多个值"
          min={0.01}
          step="any"
          value={config.budget ?? source.budget ?? ""}
          disabled={!editable}
          onChange={(event) => change({ budget: event.target.value || null })}
        />
        <p className="mt-1 text-xs text-muted-foreground">
          {source.currency} ·{" "}
          {source.budget_strategy === "ADGROUP" ? "组预算" : "系列预算"}
          {config.budget === null
            ? source.budget === null
              ? " · 按来源各组保留"
              : " · 沿用"
            : ""}
        </p>
      </TableCell>
      <TableCell className="min-w-52 max-w-64 whitespace-normal">
        {config.targeting || source.targeting ? (
          <TargetingSummary value={config.targeting ?? source.targeting} />
        ) : (
          <p className="text-sm text-muted-foreground">按来源各组定向保留</p>
        )}
        <Button
          variant="ghost"
          size="sm"
          disabled={!editable}
          onClick={() => open("targeting")}
        >
          修改定向
        </Button>
      </TableCell>
      <TableCell>
        <Button
          variant="outline"
          size="sm"
          disabled={!editable}
          onClick={() => open("materials")}
        >
          {config.material_ids.length} 条素材
        </Button>
      </TableCell>
      <TableCell>
        <Button
          variant="ghost"
          size="sm"
          disabled={!editable}
          onClick={() => open("more")}
        >
          更多设置
        </Button>
      </TableCell>
    </TableRow>
  )
}
export function CopyPreview({
  tenantId,
  bcId,
  data,
}: {
  tenantId: string
  bcId: string
  data: CopyDraftPublic
}) {
  const [expanded, setExpanded] = useState<string | null>(null)
  return (
    <Card>
      <CardHeader>
        <CardTitle>新系列预览 · {data.children.length} 个</CardTitle>
        <CardDescription>
          各副本按来源独立生成；提交会创建并启用预览中的新广告。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="max-h-96 overflow-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>来源系列</TableHead>
                <TableHead>目标账户 / 份次</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>详情</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.children.map((child) => (
                <Fragment
                  key={`${child.row_id}:${child.advertiser_id}:${child.ordinal}`}
                >
                  <TableRow>
                    <TableCell>
                      {
                        data.rows.find((row) => row.row_id === child.row_id)
                          ?.source.name
                      }
                    </TableCell>
                    <TableCell>
                      {child.advertiser_id} / {child.ordinal}
                    </TableCell>
                    <TableCell>
                      {child.status}
                      {child.error_code && (
                        <p role="alert" className="text-destructive">
                          {copyErrorLabel(child.error_code)}
                        </p>
                      )}
                    </TableCell>
                    <TableCell>
                      <div className="flex gap-2">
                        {!!child.group_settings?.length && (
                          <Button
                            variant="outline"
                            size="sm"
                            aria-expanded={expanded === child.preview_id}
                            onClick={() =>
                              setExpanded(
                                expanded === child.preview_id
                                  ? null
                                  : child.preview_id,
                              )
                            }
                          >
                            核对组设置
                          </Button>
                        )}
                        {child.preview_id && (
                          <Link
                            to="/tenants/$tenantId/build-previews/$previewId"
                            params={{ tenantId, previewId: child.preview_id }}
                            search={{ bc_id: bcId }}
                          >
                            查看预览
                          </Link>
                        )}
                        {child.submission_id && (
                          <Link
                            to="/tenants/$tenantId/build-tasks/$submissionId"
                            params={{
                              tenantId,
                              submissionId: child.submission_id,
                            }}
                            search={{ bc_id: bcId }}
                          >
                            查看搭建任务
                          </Link>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                  {expanded === child.preview_id &&
                    !!child.group_settings?.length && (
                      <TableRow>
                        <TableCell colSpan={4}>
                          <div className="flex flex-col gap-3 py-2">
                            {child.group_settings.map((group, index) => (
                              <div
                                key={`${group.name}:${index}`}
                                className="rounded-md border p-3"
                              >
                                <p className="text-sm font-medium">
                                  {group.name}
                                </p>
                                <p className="text-sm text-muted-foreground">
                                  冻结组预算：{group.budget ?? "未单独设置"} ·
                                  冻结目标 ROAS：
                                  {group.target_roas ?? "最高价值"}
                                </p>
                                {group.targeting ? (
                                  <TargetingSummary value={group.targeting} />
                                ) : (
                                  <p className="text-sm text-muted-foreground">
                                    {group.targeting_summary ||
                                      "定向详情请查看子预览"}
                                  </p>
                                )}
                              </div>
                            ))}
                          </div>
                        </TableCell>
                      </TableRow>
                    )}
                </Fragment>
              ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  )
}
