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
  Sheet,
  SheetContent,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"

type Props = {
  tenantId: string
  bcId: string
  selection: FrozenSelection | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

const DEFAULT_VALUE = "25"

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
  open,
  onOpenChange,
}: Props) {
  const navigate = useNavigate()
  const [field, setField] = useState<MutationSpec["field"]>("budget")
  const [value, setValue] = useState(DEFAULT_VALUE)
  const [excludeUnsupported, setExcludeUnsupported] = useState(false)
  const [excludedRefs, setExcludedRefs] = useState<EntityRef[]>([])
  const [includeParents, setIncludeParents] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!open) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [open])
  useEffect(() => {
    if (!open || !selection?.selection_id) return
    setExcludeUnsupported(false)
    setExcludedRefs([])
    setIncludeParents(false)
  }, [open, selection?.selection_id])
  const mutation = useMemo<MutationSpec>(
    () => ({
      field,
      mode: field === "status" ? "set" : "set",
      value: field === "status" ? "DISABLE" : value,
      include_parents: includeParents ? (selection?.refs ?? []) : [],
      excluded_refs: excludedRefs,
    }),
    [excludedRefs, field, includeParents, selection?.refs, value],
  )
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
    enabled: open && !!selection,
    queryFn: async ({ signal }) => {
      if (!selection) throw new Error("请先冻结广告选择")
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
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>管理预览</SheetTitle>
          <p className="text-sm text-muted-foreground">
            BC：{bcId} · 服务器冻结 selection，不在浏览器展开远端对象。
          </p>
        </SheetHeader>
        <div className="flex flex-col gap-4 px-4 pb-4">
          <div className="grid grid-cols-2 gap-3 text-sm">
            <label className="flex flex-col gap-1">
              操作维度
              <select
                aria-label="操作维度"
                className="h-9 rounded-md border bg-background px-2"
                value={field}
                onChange={(event) =>
                  setField(event.target.value as MutationSpec["field"])
                }
              >
                <option value="budget">预算</option>
                <option value="roas">目标 ROAS</option>
                <option value="status">投放状态</option>
              </select>
            </label>
            <div className="flex flex-col gap-1">
              <label htmlFor="management-value">新值</label>
              <Input
                id="management-value"
                aria-label="管理新值"
                value={field === "status" ? "DISABLE" : value}
                onChange={(event) => setValue(event.target.value)}
                disabled={field === "status"}
              />
            </div>
          </div>
          {preview.isPending && <p role="status">正在生成管理预览…</p>}
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
                          {item.ref.kind} · {item.ref.remote_id}
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
              !preview.data ||
              preview.isPending ||
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
