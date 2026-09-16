import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import { useEffect, useState } from "react"
import { AccountsService, type ConnectionPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { ManagementSheet } from "@/features/tenants/ManagementSheet"
import { canManage, isForbidden, RequestError } from "@/features/tenants/shared"
import { useTenantScope } from "@/features/tenants/TenantScope"
import { connectionLabels, displayTime } from "./presentation"

function authorizationName(item: ConnectionPublic) {
  const channel = item.kind === "OFFICIAL_MCP" ? "官方 MCP" : "官方 API"
  return `${item.display_name || "未命名授权"} · ${channel}`
}

export function BCAuthorization() {
  const { tenantId, bc, scope } = useTenantScope()
  const client = useQueryClient()
  const [editing, setEditing] = useState(false)
  const [selected, setSelected] = useState("")
  const query = useInfiniteQuery({
    queryKey: [
      "tenant",
      tenantId,
      "connections",
      "bc-authorization",
      bc?.bc_id,
    ],
    initialPageParam: undefined as string | undefined,
    enabled: !!tenantId && !!bc,
    queryFn: async ({ signal, pageParam }) =>
      (
        await AccountsService.getConnections({
          path: { tenant_id: tenantId! },
          query: { bc_id: bc!.bc_id, limit: 50, cursor: pageParam },
          signal,
        })
      ).data,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  })
  const items = query.data?.pages.flatMap((page) => page.items) ?? []
  const current = items.find((item) => item.id === bc?.default_connection_id)
  // 默认授权可能在后续分页；不能把第一页的第一条当成默认授权。
  useEffect(() => {
    if (
      bc?.default_connection_id &&
      !current &&
      query.hasNextPage &&
      !query.isFetching &&
      !query.error
    )
      void query.fetchNextPage()
  }, [
    bc?.default_connection_id,
    current,
    query.hasNextPage,
    query.isFetching,
    query.error,
    query.fetchNextPage,
  ])
  const mutation = useMutation({
    mutationFn: async () => {
      await AccountsService.putDefaultConnection({
        path: { tenant_id: tenantId!, bc_id: bc!.bc_id },
        body: { connection_id: selected },
      })
      // 等待服务端配置回读，账户目录再按新授权重新查询。
      await client.invalidateQueries({ queryKey: ["tenant", tenantId] })
      setEditing(false)
    },
  })
  if (!bc) return null
  if (isForbidden(query.error))
    return (
      <RequestError error={query.error} retry={() => void query.refetch()} />
    )
  const manage = canManage(scope?.role) && !isForbidden(mutation.error)
  const chosen = items.find((item) => item.id === selected)
  const dirty = selected !== (bc.default_connection_id || "")
  const exhausted = !query.isPending && !query.hasNextPage && !query.error
  const onlyOne =
    exhausted && items.filter((item) => item.status === "ACTIVE").length === 1
  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>当前 BC：{bc.name || bc.bc_id}</CardTitle>
          <CardDescription>
            统一用于账户查询、新的素材上传和广告搭建；已准备的任务继续使用原授权。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-3">
          <p className="min-w-0 break-all">
            使用授权：
            {current
              ? authorizationName(current)
              : !bc.default_connection_id
                ? "尚未设置"
                : exhausted
                  ? "原授权已不可用，请更换授权"
                  : "正在读取…"}
          </p>
          {current && (
            <span className="text-xs text-muted-foreground">
              编号尾号 {current.id.slice(-8)}
            </span>
          )}
          {current && (
            <Badge
              variant={current.status === "ACTIVE" ? "secondary" : "outline"}
            >
              {current.status === "ACTIVE"
                ? "授权正常"
                : connectionLabels[current.status]}
            </Badge>
          )}
          {manage && (
            <Button
              variant="outline"
              disabled={
                query.isPending || !!query.error || bc.ownership_conflict
              }
              onClick={() => {
                setSelected(bc.default_connection_id || "")
                mutation.reset()
                setEditing(true)
              }}
            >
              {bc.default_connection_id ? "更换授权" : "设置使用授权"}
            </Button>
          )}
          <Button variant="link" asChild>
            <Link
              to="/tenants/$tenantId/accounts"
              params={{ tenantId: tenantId! }}
              search={{ bc_id: bc.bc_id, tab: "connections" }}
            >
              管理授权
            </Link>
          </Button>
          {!manage && (
            <p className="text-sm text-muted-foreground">
              需要更换时请联系租户管理员。
            </p>
          )}
          {bc.ownership_conflict && (
            <p role="status">此 BC 存在归属冲突，请先在授权管理中处理。</p>
          )}
          {!!query.error && (
            <RequestError
              error={query.error}
              retry={() => void query.refetch()}
            />
          )}
        </CardContent>
      </Card>
      {editing && (
        <ManagementSheet
          title={`${bc.name || bc.bc_id} · 更换使用授权`}
          description="此设置用于账户查询、新的素材上传和广告搭建。已经准备好的任务继续使用原授权。"
          dirty={dirty}
          pending={mutation.isPending}
          onClose={() => setEditing(false)}
          actions={
            <Button
              disabled={
                !manage ||
                !dirty ||
                chosen?.status !== "ACTIVE" ||
                !!query.error ||
                mutation.isPending
              }
              onClick={() => mutation.mutate()}
            >
              {mutation.isPending ? "正在保存…" : "保存"}
            </Button>
          }
        >
          <FieldSet>
            <FieldLegend>请选择该 BC 使用的授权</FieldLegend>
            <FieldDescription>
              {onlyOne
                ? "当前 BC 只有一个可用授权。"
                : "只列出已关联当前 BC 的授权；API 和 MCP 是授权的接入方式。"}
              保存时会核验该授权是否仍可用于当前 BC。
            </FieldDescription>
            <RadioGroup
              value={selected}
              onValueChange={setSelected}
              disabled={mutation.isPending || !manage || !!query.error}
            >
              {items.map((item) => (
                <Field
                  key={item.id}
                  orientation="horizontal"
                  data-disabled={item.status !== "ACTIVE"}
                >
                  <RadioGroupItem
                    id={`authorization-${item.id}`}
                    value={item.id}
                    disabled={item.status !== "ACTIVE"}
                  />
                  <FieldContent>
                    <FieldLabel htmlFor={`authorization-${item.id}`}>
                      {authorizationName(item)}
                    </FieldLabel>
                    <FieldDescription>
                      编号尾号 {item.id.slice(-8)} · 最近授权：
                      {displayTime(item.last_authorized_at)}
                    </FieldDescription>
                    <div className="flex flex-wrap gap-2">
                      <Badge variant="outline">
                        {item.status === "ACTIVE"
                          ? "授权正常"
                          : connectionLabels[item.status]}
                      </Badge>
                      {item.id === bc.default_connection_id && (
                        <Badge variant="secondary">当前使用</Badge>
                      )}
                    </div>
                  </FieldContent>
                </Field>
              ))}
            </RadioGroup>
            {exhausted && !items.length && (
              <p role="status">当前 BC 暂无关联授权，请先前往授权管理接入。</p>
            )}
            {query.hasNextPage && (
              <Button
                variant="outline"
                disabled={query.isFetching || mutation.isPending}
                onClick={() => void query.fetchNextPage()}
              >
                加载更多授权
              </Button>
            )}
            {!!query.error && (
              <RequestError
                error={query.error}
                retry={() => void query.refetch()}
              />
            )}
            {!!mutation.error && (
              <RequestError
                error={mutation.error}
                retry={() => mutation.mutate()}
              />
            )}
          </FieldSet>
        </ManagementSheet>
      )}
    </>
  )
}
