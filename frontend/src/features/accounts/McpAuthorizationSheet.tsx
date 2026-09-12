import { useMutation, useQuery } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"
import { AccountsService, type McpCandidateBC } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  errorMessage,
  isForbidden,
  RequestError,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { bindingLabels } from "./presentation"

const PAGE_SIZE = 50
const MAX_DIRECTORY_BCS = 5000
const MAX_SELECTED_BCS = 1000

class BCDirectoryError extends Error {}

function canSelect(item: McpCandidateBC) {
  return (
    !item.connected &&
    (!item.binding_status || item.binding_status === "DISABLED")
  )
}

export function McpAuthorizationSheet({
  connectionId,
  attemptId,
  addBindings = false,
  ready,
  onClose,
  onSaved,
}: {
  connectionId?: string
  attemptId?: string
  addBindings?: boolean
  ready: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const { tenantId, tenant } = useTenantScope()
  const selecting = !!attemptId || addBindings
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<string[]>([])
  const initialized = useRef(false)
  const refreshRequested = useRef(false)
  const candidates = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "mcp-candidate-bcs",
      attemptId,
      connectionId,
      addBindings,
    ],
    enabled: selecting,
    retry: false,
    refetchOnWindowFocus: false,
    queryFn: async ({ signal }) => {
      const refresh = refreshRequested.current
      refreshRequested.current = false
      const items: McpCandidateBC[] = []
      let total = 0
      // 完整读取有界目录后才允许全选，避免把当前页误当作全部 BC。
      for (
        let remotePage = 1;
        remotePage <= MAX_DIRECTORY_BCS / PAGE_SIZE;
        remotePage++
      ) {
        const { data } = attemptId
          ? await AccountsService.candidateBcs({
              path: { tenant_id: tenantId!, attempt_id: attemptId },
              query: { page: remotePage, page_size: PAGE_SIZE },
              signal,
            })
          : await AccountsService.availableBcs({
              path: { tenant_id: tenantId!, connection_id: connectionId! },
              query: {
                page: remotePage,
                page_size: PAGE_SIZE,
                refresh: refresh && remotePage === 1,
              },
              signal,
            })
        if (data.total > MAX_DIRECTORY_BCS)
          throw new BCDirectoryError(
            "可访问 BC 超过 5000 个，目录无法完整读取，请联系管理员。",
          )
        if (remotePage > 1 && data.total !== total)
          throw new BCDirectoryError("可访问 BC 目录已变化，请刷新后重试。")
        total = data.total
        items.push(...data.items)
        if (items.length >= total) break
        if (!data.items.length)
          throw new BCDirectoryError("BC 目录未完整返回，请刷新后重试。")
      }
      if (
        items.length !== total ||
        new Set(items.map((item) => item.bc_id)).size !== total
      )
        throw new BCDirectoryError("BC 目录未完整返回，请刷新后重试。")
      return { items, total }
    },
  })
  useEffect(() => {
    if (!candidates.data) return
    const available = candidates.data.items
      .filter(canSelect)
      .map((item) => item.bc_id)
    // 只有唯一可接入项自动勾选；用户取消勾选后不反复替其选择。
    const autoSelect = !initialized.current && available.length === 1
    setSelected((previous) =>
      autoSelect ? available : previous.filter((id) => available.includes(id)),
    )
    initialized.current = true
  }, [candidates.data])
  const mutation = useMutation({
    mutationFn: async () => {
      if (selecting) {
        if (!selected.length || selected.length > MAX_SELECTED_BCS)
          throw new Error("每次请选择 1 至 1000 个 BC")
        const body = { bc_ids: selected }
        if (attemptId) {
          await AccountsService.binding({
            path: { tenant_id: tenantId!, attempt_id: attemptId },
            body,
          })
        } else {
          await AccountsService.addBindings({
            path: { tenant_id: tenantId!, connection_id: connectionId! },
            body,
          })
        }
        return
      }
      const { data } = await AccountsService.authorize({
        path: { tenant_id: tenantId! },
        body: { connection_id: connectionId ?? null },
      })
      const target = new URL(data.url)
      if (
        target.protocol !== "https:" ||
        target.hostname !== "business-api.tiktok.com" ||
        target.pathname !== "/portal/mcp-tt4b-authorize" ||
        target.username ||
        target.password ||
        target.port
      )
        throw new Error("授权地址无法核实")
      window.location.assign(target.href)
    },
  })
  useEffect(() => {
    // 先解除 Sheet 导航保护，再清理已消费的授权回调参数。
    if (selecting && mutation.isSuccess) onSaved()
  }, [selecting, mutation.isSuccess, onSaved])
  const available = candidates.data?.items.filter(canSelect) ?? []
  const visible =
    candidates.data?.items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE) ?? []
  return (
    <ManagementSheet
      title={
        selecting ? (addBindings ? "添加 BC" : "选择授权 BC") : "官方 MCP 授权"
      }
      description={`所属租户：${tenant?.name}`}
      dirty={false}
      pending={mutation.isPending}
      onClose={onClose}
      actions={
        <Button
          disabled={
            mutation.isPending ||
            isForbidden(mutation.error) ||
            (selecting
              ? !selected.length ||
                selected.length > MAX_SELECTED_BCS ||
                !!candidates.error ||
                candidates.isFetching
              : !ready)
          }
          onClick={() => mutation.mutate()}
        >
          {mutation.isPending
            ? "正在处理…"
            : selecting
              ? "接入并同步账户"
              : "前往 TikTok 授权"}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-sm">
          {selecting
            ? "同一授权可接入多个 BC。各 BC 独立同步账户、解绑和选择默认执行连接。"
            : "使用当前租户管理员自己的 TikTok 账号授权。返回后选择要接入的 BC，原有连接在验证完成前继续保留。"}
        </p>
        {addBindings && (
          <div className="flex flex-col gap-2">
            <p className="text-sm text-muted-foreground">
              使用此连接的现有授权读取可访问 BC。授权范围有变化时可刷新目录。
            </p>
            <Button
              variant="outline"
              disabled={
                candidates.isFetching ||
                mutation.isPending ||
                isForbidden(candidates.error)
              }
              onClick={() => {
                refreshRequested.current = true
                setPage(1)
                void candidates.refetch()
              }}
            >
              刷新可访问 BC
            </Button>
          </div>
        )}
        {selecting && (
          <>
            {candidates.isPending && <p role="status">正在读取可授权 BC…</p>}
            {candidates.error instanceof BCDirectoryError ? (
              <Alert variant="destructive">
                <AlertTitle>BC 目录未就绪</AlertTitle>
                <AlertDescription>
                  <p>{candidates.error.message}</p>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      void candidates.refetch()
                    }}
                  >
                    重试
                  </Button>
                </AlertDescription>
              </Alert>
            ) : (
              candidates.error && (
                <RequestError
                  error={candidates.error}
                  retry={() => {
                    void candidates.refetch()
                  }}
                />
              )
            )}
            {candidates.data && (
              <FieldSet>
                <FieldLegend>接入 BC</FieldLegend>
                <FieldDescription>
                  共 {candidates.data.total} 个可访问 BC，{available.length}{" "}
                  个可接入，已选 {selected.length} 个。
                </FieldDescription>
                {selected.length > MAX_SELECTED_BCS && (
                  <Alert variant="destructive">
                    <AlertTitle>所选 BC 超过单次接入上限</AlertTitle>
                    <AlertDescription>
                      每次最多接入 1000 个 BC，当前已选 {selected.length}{" "}
                      个，请减少选择后提交。
                    </AlertDescription>
                  </Alert>
                )}
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={
                      !available.length ||
                      candidates.isFetching ||
                      mutation.isPending
                    }
                    onClick={() =>
                      setSelected(available.map((item) => item.bc_id))
                    }
                  >
                    全选可接入 BC
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={!selected.length || mutation.isPending}
                    onClick={() => setSelected([])}
                  >
                    清空选择
                  </Button>
                </div>
                <FieldGroup>
                  {visible.map((item) => (
                    <Field
                      key={item.bc_id}
                      orientation="horizontal"
                      data-disabled={!canSelect(item)}
                    >
                      <Checkbox
                        id={`mcp-bc-${item.bc_id}`}
                        checked={selected.includes(item.bc_id)}
                        disabled={
                          !canSelect(item) ||
                          mutation.isPending ||
                          candidates.isFetching
                        }
                        onCheckedChange={(checked) =>
                          setSelected((previous) =>
                            checked === true
                              ? [
                                  ...previous.filter((id) => id !== item.bc_id),
                                  item.bc_id,
                                ]
                              : previous.filter((id) => id !== item.bc_id),
                          )
                        }
                      />
                      <FieldLabel htmlFor={`mcp-bc-${item.bc_id}`}>
                        {item.name || "未命名 BC"} · {item.bc_id}
                      </FieldLabel>
                      {item.binding_status && (
                        <Badge variant="outline">
                          {bindingLabels[item.binding_status]}
                        </Badge>
                      )}
                      {item.connected && !item.binding_status && (
                        <Badge variant="outline">已接入</Badge>
                      )}
                    </Field>
                  ))}
                </FieldGroup>
                {!candidates.data.total && (
                  <p className="text-sm">本次授权没有返回可接入的 BC。</p>
                )}
                {candidates.data.total > PAGE_SIZE && (
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      disabled={page === 1 || mutation.isPending}
                      onClick={() => setPage((value) => value - 1)}
                    >
                      上一页 BC
                    </Button>
                    <span className="text-sm">第 {page} 页</span>
                    <Button
                      variant="outline"
                      disabled={
                        page * PAGE_SIZE >= candidates.data.total ||
                        mutation.isPending
                      }
                      onClick={() => setPage((value) => value + 1)}
                    >
                      下一页 BC
                    </Button>
                  </div>
                )}
              </FieldSet>
            )}
          </>
        )}
        {mutation.error && (
          <Alert variant="destructive">
            <AlertTitle>操作未完成</AlertTitle>
            <AlertDescription>{errorMessage(mutation.error)}</AlertDescription>
          </Alert>
        )}
      </div>
    </ManagementSheet>
  )
}
