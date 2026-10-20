import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { useState } from "react"
import { toast } from "sonner"
import {
  AccountsService,
  type BCPublic,
  type TenantSummary,
  TenantsService,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { ProductionStatus } from "@/features/material-production/ProductionStatus"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { DirectoryPicker } from "./DirectoryPicker"
import { ManagementSheet } from "./ManagementSheet"
import {
  isForbidden,
  Pager,
  RequestError,
  roleLabels,
  ServerTable,
  StatusSelect,
  useCursorPage,
  useRetainedData,
} from "./shared"
import { PermissionPage, useTenantScope } from "./TenantScope"

type Editor =
  | { mode: "create" }
  | { mode: "edit" | "status" | "view"; tenant: TenantSummary }
export function TenantAdminPage() {
  const { user, switchTenant } = useTenantScope()
  const [input, setInput] = useState("")
  const [search, setSearch] = useState("")
  const [status, setStatus] = useState("all")
  const [editor, setEditor] = useState<Editor | null>(null)
  const paging = useCursorPage()
  const query = useQuery({
    queryKey: [
      "platform",
      "tenants",
      search,
      status,
      paging.cursor,
      paging.limit,
    ],
    enabled: user.is_superuser,
    queryFn: async ({ signal }) =>
      (
        await TenantsService.getPlatformTenants({
          query: {
            search,
            active: status === "all" ? undefined : status === "active",
            after_id: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
    placeholderData: keepPreviousData,
  })
  const data = useRetainedData(query.data, query.error)
  if (!user.is_superuser) return <PermissionPage />
  const permitted = !isForbidden(query.error)
  const columns: ColumnDef<TenantSummary>[] = [
    {
      accessorKey: "name",
      minSize: 320,
      header: "租户名称 / ID",
      cell: ({ row }) => (
        <div className="flex flex-col gap-1">
          <span className="wrap-anywhere whitespace-normal font-medium">
            {row.original.name}
          </span>
          <span className="wrap-anywhere whitespace-normal font-mono text-xs text-muted-foreground">
            {row.original.id}
          </span>
          <Button
            variant="link"
            size="sm"
            className="h-auto justify-start p-0"
            onClick={() => {
              void navigator.clipboard.writeText(row.original.id).then(
                () => toast.success("租户 ID 已复制"),
                () => toast.error("复制失败，请手动复制 ID"),
              )
            }}
          >
            复制 ID
          </Button>
        </div>
      ),
    },
    {
      accessorKey: "active",
      size: 120,
      header: "状态",
      cell: ({ row }) => (
        <Badge variant={row.original.active ? "secondary" : "outline"}>
          {row.original.active ? "正常" : "停用"}
        </Badge>
      ),
    },
    {
      id: "actions",
      size: 360,
      header: "操作",
      cell: ({ row }) => (
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setEditor({ mode: "view", tenant: row.original })}
          >
            查看
          </Button>
          {permitted && (
            <>
              <Button
                size="sm"
                variant="ghost"
                onClick={() =>
                  setEditor({ mode: "edit", tenant: row.original })
                }
              >
                编辑
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() =>
                  setEditor({ mode: "status", tenant: row.original })
                }
              >
                {row.original.active ? "停用" : "恢复"}
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={!row.original.active}
                onClick={() => switchTenant(row.original)}
              >
                进入租户
              </Button>
            </>
          )}
        </div>
      ),
    },
  ]
  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1">
          <WorkspacePageTitle>平台租户管理</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            请从租户列表点击“进入租户”，再使用该租户的投放与管理功能。
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" asChild>
            <Link to="/admin">用户管理</Link>
          </Button>
          {permitted && (
            <Button onClick={() => setEditor({ mode: "create" })}>
              新建租户
            </Button>
          )}
        </div>
      </div>
      <Card className="min-w-0">
        <CardHeader>
          <h2 className="sr-only">租户列表</h2>
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(event) => {
              event.preventDefault()
              paging.reset()
              setSearch(input.trim())
            }}
          >
            <Field className="w-full sm:w-80">
              <FieldLabel htmlFor="tenant-search">搜索租户名称或 ID</FieldLabel>
              <Input
                id="tenant-search"
                maxLength={255}
                value={input}
                onChange={(event) => setInput(event.target.value)}
                placeholder="名称或完整租户 ID"
              />
            </Field>
            <Button variant="outline" type="submit">
              搜索
            </Button>
            <StatusSelect
              value={status}
              onChange={(value) => {
                paging.reset()
                setStatus(value)
              }}
            />
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setInput("")
                setSearch("")
                setStatus("all")
                paging.reset()
              }}
            >
              清除筛选
            </Button>
          </form>
        </CardHeader>
        <CardContent className="min-w-0">
          <ServerTable
            fixedLayout={{ fillColumn: "name" }}
            rows={data?.items ?? []}
            columns={columns}
            loading={query.isPending}
            fetching={query.isFetching}
            error={query.error}
            retry={() => {
              void query.refetch()
            }}
            filtered={!!search || status !== "all"}
            emptyTitle="尚未开通租户"
          />
        </CardContent>
        <CardFooter className="block">
          <Pager
            paging={paging}
            nextCursor={data?.next_cursor}
            total={data?.total}
            busy={query.isFetching || !!query.error}
          />
        </CardFooter>
      </Card>
      {editor && (
        <TenantEditor
          key={
            editor.mode === "create"
              ? "create"
              : `${editor.mode}:${editor.tenant.id}`
          }
          editor={editor}
          onClose={() => setEditor(null)}
        />
      )}
    </>
  )
}
function TenantEditor({
  editor,
  onClose,
}: {
  editor: Editor
  onClose: () => void
}) {
  const original = editor.mode === "create" ? null : editor.tenant
  const [name, setName] = useState(original?.name ?? "")
  const [defaultBCId, setDefaultBCId] = useState(
    original?.default_bc_id ?? null,
  )
  const [defaultBCName, setDefaultBCName] = useState<string | null>(null)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [requestId] = useState(() => crypto.randomUUID())
  const [submitted, setSubmitted] = useState(false)
  const queryClient = useQueryClient()
  const isCreate = editor.mode === "create"
  const isView = editor.mode === "view"
  const isStatus = editor.mode === "status"
  const nameError = submitted && !name.trim() ? "请输入租户名称" : undefined
  const usernameError =
    submitted && isCreate && !/^[a-z0-9_.-]{3,64}$/.test(username.trim())
      ? "账号需为 3 至 64 位小写字母、数字、点、下划线或横线"
      : undefined
  const passwordError =
    submitted &&
    isCreate &&
    (password.length < 8 ||
      password.length > 128 ||
      !/[a-zA-Z]/.test(password) ||
      !/[0-9]/.test(password))
      ? "密码需为 8 至 128 位，同时包含字母和数字"
      : undefined
  const mutation = useMutation({
    mutationFn: async () =>
      isCreate
        ? (
            await TenantsService.postTenant({
              body: {
                name: name.trim(),
                username: username.trim(),
                password,
                request_id: requestId,
              },
            })
          ).data
        : (
            await TenantsService.patchTenant({
              path: { tenant_id: original!.id },
              body: isStatus
                ? { active: !original!.active }
                : {
                    name: name.trim(),
                    ...(defaultBCId !== (original?.default_bc_id ?? null)
                      ? { default_bc_id: defaultBCId }
                      : {}),
                  },
            })
          ).data,
    onSuccess: async () => {
      toast.success(isCreate ? "租户已创建，素材制作正在开通" : "租户已更新")
      setPassword("")
      onClose()
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["platform", "tenants"] }),
        queryClient.invalidateQueries({ queryKey: ["my-tenants"] }),
        queryClient.invalidateQueries({ queryKey: ["tenant"] }),
      ])
    },
  })
  const dirty =
    !isView &&
    !isStatus &&
    (name !== (original?.name ?? "") ||
      !!username ||
      !!password ||
      defaultBCId !== (original?.default_bc_id ?? null))
  const title = isCreate
    ? "新建租户"
    : isView
      ? "租户详情"
      : isStatus
        ? `${original!.active ? "停用" : "恢复"}租户`
        : "编辑租户"
  const submit = () => {
    if (isView || mutation.isPending || isForbidden(mutation.error)) return
    setSubmitted(true)
    if (!isStatus && !name.trim()) return
    if (
      isCreate &&
      (!/^[a-z0-9_.-]{3,64}$/.test(username.trim()) ||
        password.length < 8 ||
        password.length > 128 ||
        !/[a-zA-Z]/.test(password) ||
        !/[0-9]/.test(password))
    )
      return
    mutation.mutate()
  }
  return (
    <ManagementSheet
      title={title}
      description={
        original?.name ?? "创建租户及专属管理员，并开通同账号的素材制作服务。"
      }
      dirty={dirty}
      pending={mutation.isPending}
      onClose={onClose}
      actions={
        !isView && (
          <Button
            type="submit"
            form="tenant-editor"
            disabled={mutation.isPending || isForbidden(mutation.error)}
          >
            {mutation.isPending
              ? "正在保存…"
              : isCreate
                ? "创建租户"
                : isStatus
                  ? `确认${original!.active ? "停用" : "恢复"}`
                  : "保存修改"}
          </Button>
        )
      }
    >
      <form
        id="tenant-editor"
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
        className="flex flex-col gap-5"
      >
        {mutation.error && <RequestError error={mutation.error} />}
        {isView ? (
          <TenantDetails tenant={original!} />
        ) : isStatus ? (
          <p className="text-sm leading-7">
            {original!.active
              ? "停用后将阻止该租户新的系统业务操作，并暂停尚未执行的步骤。历史记录会保留，已在 TikTok 启用的广告不会自动停止。"
              : "恢复后，该租户的成员可按原有权限进入 TK-ADA。"}
          </p>
        ) : (
          <FieldGroup>
            <Field data-invalid={!!nameError}>
              <FieldLabel htmlFor="tenant-name">租户名称</FieldLabel>
              <Input
                id="tenant-name"
                maxLength={isCreate ? 100 : 120}
                value={name}
                onChange={(event) => setName(event.target.value)}
                aria-invalid={!!nameError}
                aria-describedby={nameError ? "tenant-name-error" : undefined}
                disabled={mutation.isPending}
              />
              {nameError && (
                <FieldError id="tenant-name-error">{nameError}</FieldError>
              )}
            </Field>
            {isCreate && (
              <>
                <Field data-invalid={!!usernameError}>
                  <FieldLabel htmlFor="tenant-username">登录账号</FieldLabel>
                  <Input
                    id="tenant-username"
                    value={username}
                    maxLength={64}
                    autoComplete="off"
                    disabled={mutation.isPending}
                    aria-invalid={!!usernameError}
                    onChange={(event) =>
                      setUsername(event.target.value.toLowerCase())
                    }
                  />
                  <FieldDescription>
                    该账号为租户初始管理员，两边使用相同登录账号。
                  </FieldDescription>
                  {usernameError && <FieldError>{usernameError}</FieldError>}
                </Field>
                <Field data-invalid={!!passwordError}>
                  <FieldLabel htmlFor="tenant-password">初始密码</FieldLabel>
                  <Input
                    id="tenant-password"
                    type="password"
                    value={password}
                    maxLength={128}
                    autoComplete="new-password"
                    disabled={mutation.isPending}
                    aria-invalid={!!passwordError}
                    onChange={(event) => setPassword(event.target.value)}
                  />
                  <FieldDescription>
                    两边使用相同初始密码。日后可从“素材制作”直接进入，无需再次输入。
                  </FieldDescription>
                  {passwordError && <FieldError>{passwordError}</FieldError>}
                </Field>
              </>
            )}
            {!isCreate && original && (
              <Field data-disabled={mutation.isPending}>
                <FieldLabel>默认 BC</FieldLabel>
                <DirectoryPicker<BCPublic & { id: string }>
                  label="默认 BC"
                  selectedId={defaultBCId ?? undefined}
                  valueLabel={
                    defaultBCId ? (defaultBCName ?? defaultBCId) : undefined
                  }
                  queryKey={["tenant", original.id, "default-bc-options"]}
                  disabled={mutation.isPending}
                  load={async (query, cursor, limit, signal) => {
                    const { data } = await AccountsService.getBcs({
                      path: { tenant_id: original.id },
                      query: { query, cursor, limit },
                      signal,
                    })
                    return {
                      ...data,
                      items: data.items.map((item) => ({
                        ...item,
                        id: item.bc_id,
                      })),
                    }
                  }}
                  renderItem={(item) => (
                    <span>
                      {item.name || item.bc_id} · {item.bc_id}
                    </span>
                  )}
                  onSelect={(item) => {
                    setDefaultBCId(item.bc_id)
                    setDefaultBCName(item.name || item.bc_id)
                  }}
                />
                {defaultBCId && (
                  <Button
                    type="button"
                    variant="ghost"
                    disabled={mutation.isPending}
                    onClick={() => {
                      setDefaultBCId(null)
                      setDefaultBCName(null)
                    }}
                  >
                    清除默认 BC
                  </Button>
                )}
                <FieldDescription>
                  用于外部素材推送和首次进入租户。未设置时，使用 BC
                  列表中的第一个。
                </FieldDescription>
              </Field>
            )}
          </FieldGroup>
        )}
      </form>
    </ManagementSheet>
  )
}
function TenantDetails({ tenant }: { tenant: TenantSummary }) {
  const query = useQuery({
    queryKey: ["tenant", tenant.id, "administrators"],
    enabled: tenant.active,
    queryFn: async ({ signal }) =>
      (
        await TenantsService.getMembers({
          path: { tenant_id: tenant.id },
          query: { role: "tenant_admin", active: true, limit: 50 },
          signal,
        })
      ).data,
  })
  return (
    <div className="flex flex-col gap-4">
      <p className="break-all font-mono text-sm">{tenant.id}</p>
      <Badge variant="secondary">{tenant.active ? "正常" : "停用"}</Badge>
      {tenant.active && <ProductionStatus tenantId={tenant.id} />}
      <h3 className="font-medium">租户管理员</h3>
      {!tenant.active ? (
        <p className="text-sm text-muted-foreground">恢复租户后可查看成员。</p>
      ) : query.isPending ? (
        <p role="status">正在读取管理员…</p>
      ) : query.error ? (
        <RequestError
          error={query.error}
          retry={() => {
            void query.refetch()
          }}
        />
      ) : (
        <>
          <ul className="flex flex-col gap-3">
            {query.data?.items.map((member) => (
              <li key={member.user_id} className="flex flex-col gap-1">
                <span>
                  {member.full_name || member.username} ·{" "}
                  {roleLabels[member.role]}
                </span>
                <span className="break-all text-sm text-muted-foreground">
                  {member.username}
                </span>
              </li>
            ))}
          </ul>
          {query.data?.next_cursor && (
            <p className="text-sm text-muted-foreground">
              还有更多管理员，请进入成员管理查看。
            </p>
          )}
        </>
      )}
    </div>
  )
}
