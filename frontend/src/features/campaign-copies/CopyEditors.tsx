import { useQuery } from "@tanstack/react-query"
import { useState } from "react"
import { BuildsService, type DeliveryConfig } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { AccountPicker } from "@/features/accounts/AccountPicker"
import {
  DraftSchedule,
  deliveryConfig,
  type ScheduleValue,
  scheduleError,
} from "@/features/builds/DraftSchedule"
import {
  normalizedTargeting,
  TargetingForm,
  TargetingSummary,
  targetingError,
} from "@/features/targeting/TargetingForm"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { RequestError } from "@/features/tenants/shared"
import type { CopyConfiguration, CopyRow } from "./api"

export type EditorKind =
  | "accounts"
  | "targeting"
  | "more"
  | "budget"
  | "target_roas"
  | "copies"
  | "materials"
export type Editor = { kind: EditorKind; rowIds: string[] }
const labels: Record<EditorKind, string> = {
  accounts: "目标账户",
  targeting: "受众定向",
  more: "更多设置",
  budget: "预算",
  target_roas: "目标 ROAS",
  copies: "每账户份数",
  materials: "选择素材",
}
function scheduleValue(value: DeliveryConfig): ScheduleValue {
  const local = (raw?: string | null) => {
    if (!raw) return ""
    const date = new Date(`${raw.replace(" ", "T")}Z`)
    if (Number.isNaN(date.getTime())) return ""
    const pad = (n: number) => String(n).padStart(2, "0")
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
  }
  return {
    mode: value.schedule_mode || "IMMEDIATE",
    start: local(value.schedule_start_time),
    end: local(value.schedule_end_time),
  }
}
export function CopyEditor({
  editor,
  rows,
  tenantId,
  bcId,
  onSave,
  onClose,
}: {
  editor: Editor
  rows: CopyRow[]
  tenantId: string
  bcId: string
  onSave: (patch: Partial<CopyConfiguration>) => void
  onClose: () => void
}) {
  const row = rows.find((item) => item.row_id === editor.rowIds[0])!
  const [accounts, setAccounts] = useState(
    row.configuration.target_advertiser_ids,
  )
  const [targeting, setTargeting] = useState(() =>
    normalizedTargeting(row.configuration.targeting ?? row.source.targeting),
  )
  const targetingChanged =
    JSON.stringify(targeting) !==
    JSON.stringify(
      normalizedTargeting(row.configuration.targeting ?? row.source.targeting),
    )
  const [schedule, setSchedule] = useState(() =>
    scheduleValue(row.configuration.delivery_config),
  )
  const [numeric, setNumeric] = useState(
    String(
      editor.kind === "copies"
        ? row.configuration.copies
        : editor.kind === "budget"
          ? (row.configuration.budget ?? row.source.budget ?? "")
          : (row.configuration.target_roas ?? row.source.target_roas ?? ""),
    ),
  )
  const directory = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "campaign-copies",
      bcId,
      "reference-regions",
    ],
    enabled: editor.kind === "targeting",
    queryFn: async ({ signal }) =>
      (
        await BuildsService.targetingRegions({
          path: { tenant_id: tenantId },
          query: { bc_id: bcId },
          signal,
        })
      ).data,
  })
  const error =
    editor.kind === "accounts"
      ? !accounts.length || accounts.length > 100
        ? "请选择 1–100 个账户。"
        : ""
      : editor.kind === "targeting"
        ? targetingError(targeting)
        : editor.kind === "more"
          ? scheduleError(schedule)
          : !Number.isFinite(Number(numeric)) ||
              Number(numeric) <= 0 ||
              (editor.kind === "copies" &&
                (!Number.isInteger(Number(numeric)) || Number(numeric) > 20))
            ? "请输入有效数值，份数为 1–20 的整数。"
            : ""
  function save() {
    if (error) return
    onSave(
      editor.kind === "accounts"
        ? { target_advertiser_ids: accounts }
        : editor.kind === "targeting"
          ? targetingChanged
            ? { targeting }
            : {}
          : editor.kind === "more"
            ? { delivery_config: deliveryConfig(schedule) }
            : editor.kind === "copies"
              ? { copies: Number(numeric) }
              : { [editor.kind]: numeric },
    )
  }
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{labels[editor.kind]}</DialogTitle>
          <DialogDescription>
            {editor.rowIds.length === 1
              ? row.source.name
              : `为勾选的 ${editor.rowIds.length} 条来源批量填值`}
            。保存后应用到本次复制方案。
          </DialogDescription>
        </DialogHeader>
        {editor.kind === "accounts" && (
          <FieldGroup>
            <Field>
              <FieldLabel>已选账户（{accounts.length}）</FieldLabel>
              {accounts.map((id) => (
                <div
                  key={id}
                  className="flex items-center justify-between gap-2"
                >
                  <span className="break-all font-mono text-sm">{id}</span>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`移除账户 ${id}`}
                    onClick={() =>
                      setAccounts((value) =>
                        value.filter((item) => item !== id),
                      )
                    }
                  >
                    移除
                  </Button>
                </div>
              ))}
              <AccountPicker
                tenantId={tenantId}
                bcId={bcId}
                value={accounts.join("\n")}
                disabled={false}
                onChange={(value) =>
                  setAccounts([
                    ...new Set(
                      value
                        .split("\n")
                        .map((id) => id.trim())
                        .filter(Boolean),
                    ),
                  ])
                }
              />
            </Field>
            <p className="text-sm text-muted-foreground">
              当前仅支持同币种账户复制。预览会重新核验目标权限、素材、小程序与推广链路。
            </p>
          </FieldGroup>
        )}
        {editor.kind === "targeting" && (
          <>
            {directory.error && (
              <RequestError
                error={directory.error}
                retry={() => void directory.refetch()}
              />
            )}
            {row.source.targeting_mixed && !row.configuration.targeting && (
              <Alert>
                <AlertDescription>
                  各广告组定向不同，此处修改将统一覆盖全部广告组。未修改表单直接保存会继续按来源各组保留。
                </AlertDescription>
              </Alert>
            )}
            <TargetingForm
              value={targeting}
              onChange={setTargeting}
              countries={directory.data?.region_codes ?? []}
              reference
              disabled={directory.isPending || !!directory.error}
            />
            <p className="text-sm text-muted-foreground">
              这是国家参考目录；目标账户与来源小程序的实际可投范围将在生成预览时核验。
            </p>
            <Button
              variant="outline"
              onClick={() => onSave({ targeting: null })}
            >
              恢复各来源定向
            </Button>
          </>
        )}
        {editor.kind === "more" && (
          <>
            <p className="text-sm text-muted-foreground">
              版权方、小程序、广告结构、身份、推广链接与各条广告的实际文案分别沿用来源。
            </p>
            {!!row.source.group_settings?.length && (
              <div className="flex flex-col gap-3">
                {row.source.group_settings.map((group, index) => (
                  <div
                    key={`${group.name}:${index}`}
                    className="rounded-md border p-3"
                  >
                    <p className="text-sm font-medium">{group.name}</p>
                    <p className="text-sm text-muted-foreground">
                      来源组预算：
                      {group.budget === null
                        ? "系列统一预算"
                        : `${group.budget} ${row.source.currency}`}{" "}
                      · 来源目标 ROAS：{group.target_roas ?? "最高价值"}
                    </p>
                    {group.targeting ? (
                      <TargetingSummary value={group.targeting} />
                    ) : (
                      <p className="text-sm text-muted-foreground">
                        {group.targeting_summary || "按来源实际定向保留"}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            )}
            <DraftSchedule
              value={schedule}
              onChange={setSchedule}
              disabled={false}
              error={scheduleError(schedule)}
            />
          </>
        )}
        {["budget", "target_roas", "copies"].includes(editor.kind) && (
          <Field>
            <FieldLabel htmlFor="copy-batch-value">
              {labels[editor.kind]}
            </FieldLabel>
            <Input
              id="copy-batch-value"
              type="number"
              min={editor.kind === "copies" ? 1 : 0.01}
              max={editor.kind === "copies" ? 20 : undefined}
              step={editor.kind === "copies" ? 1 : "any"}
              value={numeric}
              onChange={(event) => setNumeric(event.target.value)}
            />
            {editor.kind === "budget" && (
              <p className="text-sm text-muted-foreground">
                预算按各来源币种分别设置，当前仅支持同币种账户复制。
              </p>
            )}
          </Field>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button
            disabled={
              !!error ||
              (editor.kind === "targeting" &&
                (directory.isPending || !!directory.error))
            }
            onClick={save}
          >
            保存设置
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
export function CopyMaterials({
  row,
  onSave,
  onClose,
}: {
  row: CopyRow
  onSave: (ids: string[]) => void
  onClose: () => void
}) {
  const [selected, setSelected] = useState(row.configuration.material_ids)
  const [search, setSearch] = useState("")
  return (
    <ManagementSheet
      title="选择素材"
      description={`${row.source.name} · 仅使用此来源系列的本地素材`}
      dirty={false}
      onClose={onClose}
      actions={
        <Button disabled={!selected.length} onClick={() => onSave(selected)}>
          保存素材
        </Button>
      }
    >
      <FieldGroup>
        <Field>
          <FieldLabel htmlFor="copy-material-search">搜索素材</FieldLabel>
          <Input
            id="copy-material-search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="输入素材名称"
          />
        </Field>
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm">
            已保留 {selected.length} / {row.source.materials.length} 条
          </span>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                setSelected(
                  row.source.materials.map((item) => item.material_id),
                )
              }
            >
              全选
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSelected([])}>
              清空
            </Button>
          </div>
        </div>
        {row.source.materials
          .filter((item) =>
            item.file_name
              .toLocaleLowerCase()
              .includes(search.toLocaleLowerCase()),
          )
          .map((item) => (
            <Field key={item.material_id} orientation="horizontal">
              <Checkbox
                id={`copy-material-${item.material_id}`}
                checked={selected.includes(item.material_id)}
                onCheckedChange={(checked) =>
                  setSelected((ids) =>
                    checked
                      ? [...new Set([...ids, item.material_id])]
                      : ids.filter((id) => id !== item.material_id),
                  )
                }
              />
              <FieldLabel
                htmlFor={`copy-material-${item.material_id}`}
                className="min-w-0 flex-col items-start"
              >
                <span className="break-all">{item.file_name}</span>
                <span className="text-xs text-muted-foreground">
                  {item.media_type}
                </span>
              </FieldLabel>
            </Field>
          ))}
        {!selected.length && (
          <p role="alert" className="text-sm text-destructive">
            至少保留一条素材。
          </p>
        )}
      </FieldGroup>
    </ManagementSheet>
  )
}
