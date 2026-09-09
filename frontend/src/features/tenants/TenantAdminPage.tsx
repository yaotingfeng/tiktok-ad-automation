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
  type TenantSummary,
  TenantsService,
  type UserCandidate,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Field,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
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
      header: "租户名称 / ID",
      cell: ({ row }) => (
        <div className="flex flex-col gap-1">
          <span className="font-medium">{row.original.name}</span>
          <span className="font-mono text-xs text-muted-foreground">
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
      header: "状态",
      cell: ({ row }) => (
        <Badge variant={row.original.active ? "secondary" : "outline"}>
          {row.original.active ? "正常" : "停用"}
        </Badge>
      ),
    },
    {
      id: "actions",
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
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-2">
          <h1 className="workspace-title">平台租户管理</h1>
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
      <Card className="gap-0 py-0">
        <CardHeader className="sr-only">
          <CardTitle>租户列表</CardTitle>
        </CardHeader>
        <CardContent className="px-0">
          <form
            className="flex flex-wrap items-end gap-3 p-4"
            onSubmit={(event) => {
              event.preventDefault()
              paging.reset()
              setSearch(input.trim())
            }}
          >
            <Field className="max-w-sm">
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
          <ServerTable
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
          <Pager
            paging={paging}
            nextCursor={data?.next_cursor}
            busy={query.isFetching || !!query.error}
          />
        </CardContent>
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
  const [administrator, setAdministrator] = useState<UserCandidate | null>(null)
  const [submitted, setSubmitted] = useState(false)
  const queryClient = useQueryClient()
  const isCreate = editor.mode === "create"
  const isView = editor.mode === "view"
  const isStatus = editor.mode === "status"
  const nameError = submitted && !name.trim() ? "请输入租户名称" : undefined
  const administratorError =
    submitted && isCreate && !administrator ? "请选择初始管理员" : undefined
  const mutation = useMutation({
    mutationFn: async () =>
      isCreate
        ? (
            await TenantsService.postTenant({
              body: { name: name.trim(), administrator_id: administrator!.id },
            })
          ).data
        : (
            await TenantsService.patchTenant({
              path: { tenant_id: original!.id },
              body: isStatus
                ? { active: !original!.active }
                : { name: name.trim() },
            })
          ).data,
    onSuccess: async () => {
      toast.success(isCreate ? "租户已创建" : "租户已更新")
      onClose()
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["platform", "tenants"] }),
        queryClient.invalidateQueries({ queryKey: ["my-tenants"] }),
        queryClient.invalidateQueries({ queryKey: ["tenant"] }),
      ])
    },
  })
  const dirty =
    !isView && !isStatus && (name !== (original?.name ?? "") || !!administrator)
  const title = isCreate
    ? "新建租户"
    : isView
      ? "租户详情"
      : isStatus
        ? `${original!.active ? "停用" : "恢复"}租户`
        : "编辑租户"
  const submit = () => {
    if (mutation.isPending || isForbidden(mutation.error)) return
    setSubmitted(true)
    if (!isStatus && (!name.trim() || (isCreate && !administrator))) return
    mutation.mutate()
  }
  return (
    <ManagementSheet
      title={title}
      description={original?.name ?? "设置租户名称和初始管理员。"}
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
              : "恢复后，该租户的成员可按原有权限进入工作台。"}
          </p>
        ) : (
          <FieldGroup>
            <Field data-invalid={!!nameError}>
              <FieldLabel htmlFor="tenant-name">租户名称</FieldLabel>
              <Input
                id="tenant-name"
                maxLength={120}
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
              <Field data-invalid={!!administratorError}>
                <FieldLabel>初始管理员</FieldLabel>
                <DirectoryPicker<UserCandidate>
                  label="初始管理员"
                  valueLabel={
                    administrator
                      ? `${administrator.full_name || administrator.email} · ${administrator.email}`
                      : undefined
                  }
                  queryKey={["platform", "user-candidates"]}
                  requiredSearch
                  disabled={mutation.isPending}
                  invalid={!!administratorError}
                  describedBy={
                    administratorError ? "administrator-error" : undefined
                  }
                  load={async (query, cursor, limit, signal) =>
                    (
                      await TenantsService.getPlatformUserCandidates({
                        query: { query, after_id: cursor, limit },
                        signal,
                      })
                    ).data
                  }
                  renderItem={(item) => (
                    <>
                      <span>{item.full_name || item.email}</span>
                      <span className="text-xs text-muted-foreground">
                        {item.email}
                      </span>
                    </>
                  )}
                  onSelect={setAdministrator}
                />
                {administrator && (
                  <p className="break-all font-mono text-xs text-muted-foreground">
                    {administrator.id}
                  </p>
                )}
                {administratorError && (
                  <FieldError id="administrator-error">
                    {administratorError}
                  </FieldError>
                )}
                <p className="text-xs text-muted-foreground">
                  从已有启用用户中选择。新账号由平台用户管理开通。
                </p>
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
                  {member.full_name || member.email} · {roleLabels[member.role]}
                </span>
                <span className="break-all text-sm text-muted-foreground">
                  {member.email}
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
