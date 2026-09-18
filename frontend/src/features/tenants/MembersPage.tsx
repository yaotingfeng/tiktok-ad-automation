import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useState } from "react"
import { toast } from "sonner"
import {
  type MemberPublic,
  type MemberSet,
  TenantsService,
  type UserCandidate,
} from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card"
import {
  Field,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import { DirectoryPicker } from "./DirectoryPicker"
import { ManagementSheet } from "./ManagementSheet"
import {
  canManage,
  isForbidden,
  memberRoles,
  Pager,
  RequestError,
  roleLabels,
  ServerTable,
  StatusSelect,
  useCursorPage,
  useRetainedData,
} from "./shared"
import { PermissionPage, useTenantScope } from "./TenantScope"

export function MembersPage() {
  const { scope, tenant } = useTenantScope()
  const tenantId = scope!.tenantId
  const permitted = canManage(scope?.role)
  const [input, setInput] = useState("")
  const [search, setSearch] = useState("")
  const [status, setStatus] = useState("all")
  const [role, setRole] = useState("all")
  const [editor, setEditor] = useState<MemberPublic | "new" | null>(null)
  const paging = useCursorPage()
  const query = useQuery({
    queryKey: [
      "tenant",
      tenantId,
      "members",
      search,
      status,
      role,
      paging.cursor,
      paging.limit,
    ],
    enabled: permitted,
    queryFn: async ({ signal }) =>
      (
        await TenantsService.getMembers({
          path: { tenant_id: tenantId },
          query: {
            search,
            active: status === "all" ? undefined : status === "active",
            role: role === "all" ? undefined : (role as MemberSet["role"]),
            after_id: paging.cursor,
            limit: paging.limit,
          },
          signal,
        })
      ).data,
    placeholderData: keepPreviousData,
  })
  const data = useRetainedData(query.data, query.error)
  if (!permitted) return <PermissionPage />
  const writable = !isForbidden(query.error)
  const columns: ColumnDef<MemberPublic>[] = [
    {
      accessorKey: "username",
      minSize: 320,
      header: "姓名 / 账号",
      cell: ({ row }) => (
        <div className="flex flex-col gap-1">
          <span className="wrap-anywhere whitespace-normal font-medium">
            {row.original.full_name || "未设置姓名"}
          </span>
          <span className="wrap-anywhere whitespace-normal">
            {row.original.username}
          </span>
          <span className="wrap-anywhere whitespace-normal font-mono text-xs text-muted-foreground">
            {row.original.user_id}
          </span>
        </div>
      ),
    },
    {
      accessorKey: "role",
      size: 144,
      header: "角色",
      cell: ({ row }) => (
        <Badge variant="secondary">{roleLabels[row.original.role]}</Badge>
      ),
    },
    {
      accessorKey: "active",
      size: 168,
      header: "状态",
      cell: ({ row }) => (
        <div className="flex flex-wrap gap-1">
          <Badge variant={row.original.active ? "secondary" : "outline"}>
            {row.original.active ? "成员正常" : "成员停用"}
          </Badge>
          {!row.original.user_active && (
            <Badge variant="outline">用户账号已停用</Badge>
          )}
        </div>
      ),
    },
    {
      id: "actions",
      size: 144,
      header: "操作",
      cell: ({ row }) =>
        writable && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => setEditor(row.original)}
          >
            编辑成员
          </Button>
        ),
    },
  ]
  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1">
          <WorkspacePageTitle>成员管理</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            {tenant!.name} · 管理当前租户的成员与固定角色。
          </p>
        </div>
        {writable && <Button onClick={() => setEditor("new")}>添加成员</Button>}
      </div>
      <Card className="min-w-0">
        <CardHeader>
          <h2 className="sr-only">成员列表</h2>
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(event) => {
              event.preventDefault()
              paging.reset()
              setSearch(input.trim())
            }}
          >
            <Field className="w-full sm:w-80">
              <FieldLabel htmlFor="member-search">
                搜索成员姓名或账号
              </FieldLabel>
              <Input
                id="member-search"
                maxLength={255}
                value={input}
                onChange={(event) => setInput(event.target.value)}
              />
            </Field>
            <Button type="submit" variant="outline">
              搜索
            </Button>
            <Select
              value={role}
              onValueChange={(value) => {
                paging.reset()
                setRole(value)
              }}
            >
              <SelectTrigger aria-label="角色筛选">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="all">全部角色</SelectItem>
                  {memberRoles.map((value) => (
                    <SelectItem key={value} value={value}>
                      {roleLabels[value]}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
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
                setRole("all")
                paging.reset()
              }}
            >
              清除筛选
            </Button>
          </form>
        </CardHeader>
        <CardContent className="min-w-0">
          <ServerTable
            fixedLayout={{ fillColumn: "username" }}
            rows={data?.items ?? []}
            columns={columns}
            loading={query.isPending}
            fetching={query.isFetching}
            error={query.error}
            retry={() => {
              void query.refetch()
            }}
            filtered={!!search || status !== "all" || role !== "all"}
            emptyTitle="尚无成员记录"
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
        <MemberEditor
          key={editor === "new" ? "new" : editor.user_id}
          member={editor === "new" ? null : editor}
          tenantId={tenantId}
          tenantName={tenant!.name}
          onClose={() => setEditor(null)}
        />
      )}
    </>
  )
}
function MemberEditor({
  member,
  tenantId,
  tenantName,
  onClose,
}: {
  member: MemberPublic | null
  tenantId: string
  tenantName: string
  onClose: () => void
}) {
  const [mode, setMode] = useState<"create" | "existing">("create")
  const [user, setUser] = useState<UserCandidate | null>(
    member
      ? {
          id: member.user_id,
          username: member.username,
          full_name: member.full_name,
        }
      : null,
  )
  const [role, setRole] = useState<MemberSet["role"]>(
    member?.role ?? "operator",
  )
  const [active, setActive] = useState(member?.active ?? true)
  const [username, setUsername] = useState("")
  const [fullName, setFullName] = useState("")
  const [password, setPassword] = useState("")
  const [confirmation, setConfirmation] = useState("")
  const [submitted, setSubmitted] = useState(false)
  const queryClient = useQueryClient()
  const creating = !member && mode === "create"
  const normalizedUsername = username.trim().toLowerCase()
  const mutation = useMutation({
    mutationFn: async () => {
      if (creating)
        return (
          await TenantsService.postMemberUser({
            path: { tenant_id: tenantId },
            body: {
              username: normalizedUsername,
              full_name: fullName.trim() || null,
              password,
              role,
            },
          })
        ).data
      return (
        await TenantsService.putMember({
          path: { tenant_id: tenantId },
          body: { user_id: user!.id, role, active },
        })
      ).data
    },
    onSuccess: async () => {
      toast.success(
        member ? "成员已更新" : creating ? "用户已创建并添加" : "成员已添加",
      )
      onClose()
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["tenant", tenantId] }),
        queryClient.invalidateQueries({ queryKey: ["my-tenants"] }),
      ])
    },
  })
  const dirty = member
    ? (!!user &&
        (!member ||
          user.id !== member.user_id ||
          role !== member.role ||
          active !== member.active)) ||
      role !== (member?.role ?? "operator") ||
      active !== (member?.active ?? true)
    : !!username ||
      !!fullName ||
      !!password ||
      !!confirmation ||
      !!user ||
      role !== "operator" ||
      !active
  const userError =
    submitted && !creating && !user ? "请选择已有用户" : undefined
  const usernameInvalid =
    creating && !/^[a-z0-9_.-]{3,64}$/.test(normalizedUsername)
  const passwordInvalid =
    creating && (password.length < 8 || password.length > 128)
  const confirmationInvalid = creating && password !== confirmation
  const usernameError =
    submitted && usernameInvalid
      ? "账号须为 3 至 64 位小写字母、数字、点、下划线或连字符"
      : undefined
  const passwordError =
    submitted && passwordInvalid ? "密码须为 8 至 128 个字符" : undefined
  const confirmationError =
    submitted && confirmationInvalid ? "两次输入的密码不一致" : undefined
  const valid = creating
    ? !usernameInvalid && !passwordInvalid && !confirmationInvalid
    : !!user
  return (
    <ManagementSheet
      title={member ? "编辑成员" : "添加成员"}
      description={
        member
          ? `${tenantName} · 修改仅影响该租户的成员关系。`
          : `${tenantName} · 新账号会直接创建并绑定到当前租户。`
      }
      dirty={dirty}
      pending={mutation.isPending}
      onClose={onClose}
      actions={
        <Button
          form="member-editor"
          type="submit"
          disabled={mutation.isPending || isForbidden(mutation.error)}
        >
          {mutation.isPending
            ? "正在保存…"
            : creating
              ? "创建并添加"
              : "保存成员"}
        </Button>
      }
    >
      <form
        id="member-editor"
        noValidate
        onSubmit={(event) => {
          event.preventDefault()
          if (mutation.isPending || isForbidden(mutation.error)) return
          setSubmitted(true)
          if (valid) mutation.mutate()
        }}
        className="flex flex-col gap-5"
      >
        {mutation.error && <RequestError error={mutation.error} />}
        <FieldGroup>
          {member ? (
            <ExistingUserField
              member={member}
              tenantId={tenantId}
              user={user}
              setUser={setUser}
              userError={userError}
              disabled={mutation.isPending}
            />
          ) : (
            <Tabs
              value={mode}
              onValueChange={(value) => {
                setMode(value as "create" | "existing")
                setSubmitted(false)
                mutation.reset()
              }}
            >
              <TabsList className="grid w-full grid-cols-2">
                <TabsTrigger value="create">新建用户</TabsTrigger>
                <TabsTrigger value="existing">选择已有用户</TabsTrigger>
              </TabsList>
              <TabsContent value="create" className="flex flex-col gap-5 pt-3">
                <Field data-invalid={!!usernameError}>
                  <FieldLabel htmlFor="member-username">登录账号</FieldLabel>
                  <Input
                    id="member-username"
                    value={username}
                    maxLength={64}
                    autoComplete="off"
                    disabled={mutation.isPending}
                    onChange={(event) => setUsername(event.target.value)}
                  />
                  {usernameError && <FieldError>{usernameError}</FieldError>}
                </Field>
                <Field>
                  <FieldLabel htmlFor="member-full-name">
                    姓名（选填）
                  </FieldLabel>
                  <Input
                    id="member-full-name"
                    value={fullName}
                    maxLength={255}
                    disabled={mutation.isPending}
                    onChange={(event) => setFullName(event.target.value)}
                  />
                </Field>
                <Field data-invalid={!!passwordError}>
                  <FieldLabel htmlFor="member-password">初始密码</FieldLabel>
                  <Input
                    id="member-password"
                    type="password"
                    value={password}
                    minLength={8}
                    maxLength={128}
                    autoComplete="new-password"
                    disabled={mutation.isPending}
                    onChange={(event) => setPassword(event.target.value)}
                  />
                  {passwordError && <FieldError>{passwordError}</FieldError>}
                </Field>
                <Field data-invalid={!!confirmationError}>
                  <FieldLabel htmlFor="member-password-confirmation">
                    确认密码
                  </FieldLabel>
                  <Input
                    id="member-password-confirmation"
                    type="password"
                    value={confirmation}
                    minLength={8}
                    maxLength={128}
                    autoComplete="new-password"
                    disabled={mutation.isPending}
                    onChange={(event) => setConfirmation(event.target.value)}
                  />
                  {confirmationError && (
                    <FieldError>{confirmationError}</FieldError>
                  )}
                </Field>
              </TabsContent>
              <TabsContent value="existing" className="pt-3">
                <ExistingUserField
                  tenantId={tenantId}
                  user={user}
                  setUser={setUser}
                  userError={userError}
                  disabled={mutation.isPending}
                />
              </TabsContent>
            </Tabs>
          )}
          <Field>
            <FieldLabel htmlFor="member-role">租户角色</FieldLabel>
            <Select
              value={role}
              onValueChange={(value) => setRole(value as MemberSet["role"])}
              disabled={mutation.isPending}
            >
              <SelectTrigger id="member-role">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {memberRoles.map((value) => (
                    <SelectItem key={value} value={value}>
                      {roleLabels[value]}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          {!creating && (
            <Field>
              <FieldLabel htmlFor="member-status">成员状态</FieldLabel>
              <Select
                value={active ? "active" : "inactive"}
                onValueChange={(value) => setActive(value === "active")}
                disabled={mutation.isPending}
              >
                <SelectTrigger id="member-status">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectItem value="active">正常</SelectItem>
                    <SelectItem value="inactive">停用</SelectItem>
                  </SelectGroup>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                停用后不可访问本租户，其他租户权限保持不变。
              </p>
            </Field>
          )}
        </FieldGroup>
      </form>
    </ManagementSheet>
  )
}

function ExistingUserField({
  member,
  tenantId,
  user,
  setUser,
  userError,
  disabled,
}: {
  member?: MemberPublic
  tenantId: string
  user: UserCandidate | null
  setUser: (user: UserCandidate) => void
  userError?: string
  disabled: boolean
}) {
  return (
    <Field data-invalid={!!userError}>
      <FieldLabel>已有用户</FieldLabel>
      {member ? (
        <p className="break-all text-sm">
          {member.full_name || member.username} · {member.username}
        </p>
      ) : (
        <DirectoryPicker<UserCandidate>
          label="已有用户"
          valueLabel={
            user
              ? `${user.full_name || user.username} · ${user.username}`
              : undefined
          }
          queryKey={["tenant", tenantId, "member-candidates"]}
          requiredSearch
          invalid={!!userError}
          describedBy={userError ? "member-user-error" : undefined}
          disabled={disabled}
          load={async (query, cursor, limit, signal) =>
            (
              await TenantsService.getMemberCandidates({
                path: { tenant_id: tenantId },
                query: { query, after_id: cursor, limit },
                signal,
              })
            ).data
          }
          renderItem={(candidate) => (
            <>
              <span>{candidate.full_name || candidate.username}</span>
              <span className="text-xs text-muted-foreground">
                {candidate.username}
              </span>
            </>
          )}
          onSelect={setUser}
        />
      )}
      {user && (
        <p className="break-all font-mono text-xs text-muted-foreground">
          {user.id}
        </p>
      )}
      {userError && <FieldError id="member-user-error">{userError}</FieldError>}
    </Field>
  )
}
