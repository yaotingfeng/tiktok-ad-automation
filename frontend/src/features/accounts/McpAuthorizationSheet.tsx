import { useMutation, useQuery } from "@tanstack/react-query"
import { useEffect, useState } from "react"
import { AccountsService, type McpCandidateBC } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import {
  errorMessage,
  isForbidden,
  RequestError,
} from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"

export function McpAuthorizationSheet({
  connectionId,
  attemptId,
  ready,
  onClose,
  onSaved,
}: {
  connectionId?: string
  attemptId?: string
  ready: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const { tenantId, tenant } = useTenantScope()
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<McpCandidateBC | null>(null)
  const candidates = useQuery({
    queryKey: ["tenant", tenantId, "mcp-candidate-bcs", attemptId, page],
    enabled: !!attemptId,
    retry: false,
    queryFn: async ({ signal }) =>
      (
        await AccountsService.candidateBcs({
          path: { tenant_id: tenantId!, attempt_id: attemptId! },
          query: { page, page_size: 50 },
          signal,
        })
      ).data,
  })
  const mutation = useMutation({
    mutationFn: async () => {
      if (attemptId) {
        if (!selected) return
        await AccountsService.binding({
          path: { tenant_id: tenantId!, attempt_id: attemptId },
          body: { bc_id: selected.bc_id },
        })
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
      ) {
        throw new Error("授权地址无法核实")
      }
      window.location.assign(target.href)
    },
  })
  useEffect(() => {
    // 完成态渲染先解除 Sheet 导航保护，再清理已消费的授权回调参数。
    if (attemptId && mutation.isSuccess) onSaved()
  }, [attemptId, mutation.isSuccess, onSaved])
  return (
    <ManagementSheet
      title={attemptId ? "选择授权 BC" : "官方 MCP 授权"}
      description={`所属租户：${tenant?.name}`}
      dirty={false}
      pending={mutation.isPending}
      onClose={onClose}
      actions={
        <Button
          disabled={
            mutation.isPending ||
            isForbidden(mutation.error) ||
            (attemptId ? !selected || !!candidates.error : !ready)
          }
          onClick={() => mutation.mutate()}
        >
          {mutation.isPending
            ? "正在处理…"
            : attemptId
              ? "绑定并发现账户"
              : "前往 TikTok 授权"}
        </Button>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-sm">
          {attemptId
            ? "每条 MCP 连接绑定一个 BC。选择后系统核对完整账户目录，验证完成后连接才生效。"
            : "使用当前租户管理员自己的 TikTok 账号授权。返回后选择要绑定的 BC，原有连接在验证完成前继续保留。"}
        </p>
        {attemptId && (
          <>
            {candidates.isPending && <p role="status">正在读取可授权 BC…</p>}
            {candidates.error && (
              <RequestError
                error={candidates.error}
                retry={() => {
                  void candidates.refetch()
                }}
              />
            )}
            {candidates.data && (
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor="mcp-bc">绑定 BC</FieldLabel>
                  <Select
                    value={selected?.bc_id ?? ""}
                    onValueChange={(value) =>
                      setSelected(
                        candidates.data.items.find(
                          (item) => item.bc_id === value,
                        ) ?? null,
                      )
                    }
                  >
                    <SelectTrigger id="mcp-bc">
                      <SelectValue placeholder="请选择 BC">
                        {selected
                          ? `${selected.name || "未命名 BC"} · ${selected.bc_id}`
                          : undefined}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {candidates.data.items.map((item) => (
                          <SelectItem key={item.bc_id} value={item.bc_id}>
                            {item.name || "未命名 BC"} · {item.bc_id}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                  <FieldDescription>
                    {candidates.data.total
                      ? `共 ${candidates.data.total} 个可选 BC`
                      : "本次授权没有返回可绑定的 BC。"}
                  </FieldDescription>
                </Field>
                {candidates.data.total > 50 && (
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      disabled={page === 1 || candidates.isFetching}
                      onClick={() => setPage((value) => value - 1)}
                    >
                      上一页 BC
                    </Button>
                    <span className="text-sm">第 {page} 页</span>
                    <Button
                      variant="outline"
                      disabled={
                        page * 50 >= candidates.data.total ||
                        candidates.isFetching
                      }
                      onClick={() => setPage((value) => value + 1)}
                    >
                      下一页 BC
                    </Button>
                  </div>
                )}
              </FieldGroup>
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
