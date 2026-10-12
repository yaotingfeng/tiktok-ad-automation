import { useMutation, useQuery } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useEffect, useMemo, useState } from "react"
import {
  AdManagementService,
  type EntityRef,
  type FrozenSelection,
  type ManagementPreviewPublic,
  type MutationSpec,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Sheet,
  SheetContent,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"

// 工具栏选定的批量动作：启停直接带状态值，数值类动作在面板里再填调整方式与数值。
export type ManagementAction =
  | { field: "status"; value: "ENABLE" | "DISABLE" }
  | { field: "roas" | "budget" }

type Props = {
  tenantId: string
  bcId: string
  selection: FrozenSelection | null
  action: ManagementAction | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

type MutationMode = MutationSpec["mode"]

const MODE_OPTIONS: Array<{ value: MutationMode; label: string }> = [
  { value: "set", label: "设为" },
  { value: "add", label: "增加" },
  { value: "subtract", label: "减少" },
  { value: "increase_percent", label: "增加百分比" },
  { value: "decrease_percent", label: "减少百分比" },
]

const KIND_LABELS: Record<EntityRef["kind"], string> = {
  campaign: "系列",
  adgroup: "广告组",
  ad: "广告",
  creative: "素材",
}

// 数值输入停顿后才请求预览，避免每敲一个字符都让服务器计算一次。
const VALUE_DEBOUNCE_MS = 400

/** 动作的稳定键：调用方用它（连同冻结选择 ID）作为面板 key，换动作即重新挂载。 */
export function managementActionKey(action: ManagementAction | null) {
  if (!action) return ""
  return action.field === "status" ? `status:${action.value}` : action.field
}

export function actionTitle(action: ManagementAction | null) {
  if (!action) return "管理预览"
  if (action.field === "status")
    return action.value === "ENABLE" ? "批量启用" : "批量暂停"
  return action.field === "roas" ? "修改目标 ROAS" : "修改预算"
}

// 只接受大于 0 的普通十进制数；"0x10"、"1e3" 之类后端 Decimal 不认的写法一律拒绝。
const PLAIN_DECIMAL = /^\d+(\.\d+)?$/
function validNumber(value: string) {
  const trimmed = value.trim()
  return PLAIN_DECIMAL.test(trimmed) && Number(trimmed) > 0
}

function remaining(expiresAt?: string) {
  if (!expiresAt) return ""
  const seconds = Math.max(
    0,
    Math.ceil((Date.parse(expiresAt) - Date.now()) / 1000),
  )
  if (seconds <= 0) return "预览已过期"
  return `五分钟内有效 · 剩余 ${Math.floor(seconds / 60)}分${seconds % 60}秒`
}

export function ManagementPreviewSheet({
  tenantId,
  bcId,
  selection,
  action,
  open,
  onOpenChange,
}: Props) {
  const navigate = useNavigate()
  const [mode, setMode] = useState<MutationMode>("set")
  // input 是输入框里的原文，value 是防抖后用于生成预览的数值。
  const [input, setInput] = useState("")
  const [value, setValue] = useState("")
  const [excludeUnsupported, setExcludeUnsupported] = useState(false)
  const [excludedRefs, setExcludedRefs] = useState<EntityRef[]>([])
  const [includeParents, setIncludeParents] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!open) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [open])
  // 面板状态不在这里手动清空：调用方按"冻结选择 + 动作"给面板换 key，
  // 每次重新打开都是全新挂载，旧的方式、数值、排除项和父级联动不可能被提交。
  useEffect(() => {
    if (input === value) return
    const timer = window.setTimeout(() => setValue(input), VALUE_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [input, value])
  const numeric = action !== null && action.field !== "status"
  const mutation = useMemo<MutationSpec | null>(() => {
    if (!action) return null
    const includes = includeParents ? (selection?.refs ?? []) : []
    if (action.field === "status")
      return {
        field: "status",
        mode: "set",
        value: action.value,
        include_parents: includes,
        excluded_refs: excludedRefs,
      }
    // 数值为空或非法时不生成预览，避免用默认值误改真实广告。
    if (!validNumber(value)) return null
    return {
      field: action.field,
      mode,
      value: value.trim(),
      include_parents: includes,
      excluded_refs: excludedRefs,
    }
  }, [action, mode, value, includeParents, excludedRefs, selection?.refs])
  const preview = useQuery<ManagementPreviewPublic>({
    queryKey: [
      "tenant",
      tenantId,
      "ad-management-preview",
      selection?.selection_id,
      bcId,
      mutation,
      excludeUnsupported,
      includeParents,
    ],
    enabled: open && !!selection && !!mutation,
    queryFn: async ({ signal }) => {
      if (!selection || !mutation) throw new Error("请先冻结广告选择")
      return (
        await AdManagementService.preparePreview({
          path: { tenant_id: tenantId },
          body: {
            selection_id: selection.selection_id,
            bc_id: bcId,
            mutation,
          },
          signal,
        })
      ).data
    },
    refetchOnWindowFocus: false,
  })
  const submit = useMutation({
    mutationFn: async () => {
      const data = preview.data
      if (!data) throw new Error("预览尚未完成")
      return (
        await AdManagementService.submitTask({
          path: { tenant_id: tenantId },
          body: {
            preview_id: data.preview_id,
            preview_digest: data.digest,
            idempotency_key: crypto.randomUUID(),
          },
        })
      ).data
    },
    onSuccess: (task) => {
      onOpenChange(false)
      void navigate({
        to: "/tenants/$tenantId/ad-management-tasks/$taskId",
        params: { tenantId, taskId: task.task_id },
        search: { bc_id: task.bc_id },
      })
    },
  })
  const unsupported =
    preview.data?.items?.filter((item) =>
      ["UNSUPPORTED", "CONFLICT"].includes(item.execution_result ?? ""),
    ) ?? []
  const expiresAt = preview.data?.expires_at
  const expired = expiresAt ? Date.parse(expiresAt) <= now : false
  // 输入还在防抖期内时，屏幕上的预览对应的是旧数值，不能提交。
  const settling = numeric && input !== value
  const needsValue = numeric && !validNumber(input)
  const percent = mode === "increase_percent" || mode === "decrease_percent"
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{actionTitle(action)}</SheetTitle>
          <p className="text-sm text-muted-foreground">
            BC：{bcId} · 服务器冻结 selection，不在浏览器展开远端对象。
          </p>
        </SheetHeader>
        <div className="flex flex-col gap-4 px-4 pb-4">
          {action?.field === "status" && (
            <p className="text-sm">
              将所选对象的投放状态设为
              {action.value === "ENABLE" ? "启用" : "暂停"}。
            </p>
          )}
          {numeric && (
            <div className="grid grid-cols-2 gap-3 text-sm">
              <div className="flex flex-col gap-1">
                <span>调整方式</span>
                <Select
                  value={mode}
                  onValueChange={(next) => setMode(next as MutationMode)}
                >
                  <SelectTrigger aria-label="调整方式" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {MODE_OPTIONS.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex flex-col gap-1">
                <label htmlFor="management-value">数值</label>
                <div className="flex items-center gap-2">
                  <Input
                    id="management-value"
                    aria-label="管理新值"
                    inputMode="decimal"
                    value={input}
                    onChange={(event) => setInput(event.target.value)}
                  />
                  {percent && <span className="text-muted-foreground">%</span>}
                </div>
                {needsValue && (
                  <p className="text-xs text-muted-foreground">
                    请输入大于 0 的数值
                  </p>
                )}
              </div>
            </div>
          )}
          {preview.isLoading && (
            <p role="status" aria-label="正在生成管理预览">
              正在生成管理预览…
            </p>
          )}
          {preview.error && (
            <Alert variant="destructive">
              <AlertDescription>预览生成失败，请重新尝试。</AlertDescription>
            </Alert>
          )}
          {preview.data && (
            <>
              <div className="rounded-md border p-3 text-sm">
                <div className="mb-2 grid gap-1 text-muted-foreground">
                  <span>连接：{preview.data.route.connection_id}</span>
                  <span>
                    账户：
                    {[
                      ...new Set(
                        (selection?.refs ?? []).map((ref) => ref.advertiser_id),
                      ),
                    ].join("、") || "—"}
                  </span>
                </div>
                <div className="flex flex-wrap gap-4">
                  <span>选中 {preview.data.counts?.selected ?? 0}</span>
                  <span>目标 {preview.data.counts?.targets ?? 0}</span>
                  <span>联动 {preview.data.counts?.linked ?? 0}</span>
                  <span>不可用 {preview.data.counts?.unsupported ?? 0}</span>
                </div>
                <p className="mt-2 text-muted-foreground">
                  {remaining(expiresAt)}
                </p>
              </div>
              <div className="flex items-start gap-2 rounded-md border p-3 text-sm">
                <Checkbox
                  id="include-parents"
                  checked={includeParents}
                  onCheckedChange={(checked) =>
                    setIncludeParents(checked === true)
                  }
                  aria-label="包括父级联动"
                />
                <label htmlFor="include-parents">
                  包括父级联动（{selection?.refs?.length ?? 0} 个候选）
                </label>
              </div>
              {unsupported.length > 0 && (
                <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm">
                  <Checkbox
                    id="exclude-unsupported"
                    checked={excludeUnsupported}
                    onCheckedChange={(checked) => {
                      const next = checked === true
                      setExcludeUnsupported(next)
                      if (next)
                        setExcludedRefs(
                          (preview.data?.items ?? [])
                            .filter((item) =>
                              ["UNSUPPORTED", "CONFLICT"].includes(
                                item.execution_result ?? "",
                              ),
                            )
                            .map((item) => item.ref),
                        )
                      else setExcludedRefs([])
                    }}
                    aria-label="排除不可用项"
                  />
                  <label htmlFor="exclude-unsupported">
                    排除不可用项后才可提交（{unsupported.length} 项）
                  </label>
                </div>
              )}
              <div className="max-h-72 overflow-auto rounded-md border">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left">
                      <th className="p-2">目标</th>
                      <th className="p-2">原值</th>
                      <th className="p-2">新值</th>
                      <th className="p-2">结果</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(preview.data.items ?? []).map((item) => (
                      <tr
                        key={`${item.ref.kind}:${item.ref.remote_id}`}
                        className="border-b"
                      >
                        <td
                          className="max-w-48 truncate p-2"
                          title={item.ref.remote_id}
                        >
                          {KIND_LABELS[item.ref.kind] ?? item.ref.kind} ·{" "}
                          {item.ref.remote_id}
                        </td>
                        <td className="p-2">{item.original_value ?? "—"}</td>
                        <td className="p-2">{item.final_value ?? "—"}</td>
                        <td className="p-2">
                          {item.reason ?? item.execution_result ?? "待处理"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
        <SheetFooter>
          <Button
            disabled={
              !mutation ||
              !preview.data ||
              preview.isFetching ||
              settling ||
              submit.isPending ||
              expired ||
              (unsupported.length > 0 && !excludeUnsupported)
            }
            onClick={() => submit.mutate()}
          >
            {submit.isPending ? "提交中…" : "提交管理任务"}
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}
