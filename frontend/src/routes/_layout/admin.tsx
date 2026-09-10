import { useSuspenseQuery } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { Suspense } from "react"
import { ErrorBoundary } from "react-error-boundary"
import { type UserPublic, UsersService } from "@/client"
import AddUser from "@/components/Admin/AddUser"
import { columns, type UserTableData } from "@/components/Admin/columns"
import { DataTable } from "@/components/Common/DataTable"
import PendingUsers from "@/components/Pending/PendingUsers"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Card, CardContent } from "@/components/ui/card"
import { WorkspacePageTitle } from "@/features/workspace/WorkspacePageTitle"
import useAuth from "@/hooks/useAuth"

function getUsersQueryOptions() {
  return {
    queryFn: async () =>
      (await UsersService.readUsers({ query: { skip: 0, limit: 100 } })).data,
    queryKey: ["users"],
  }
}

export const Route = createFileRoute("/_layout/admin")({
  component: Admin,
  head: () => ({
    meta: [
      {
        title: "平台管理 · TK-ADA",
      },
    ],
  }),
})

function UsersTableContent() {
  const { user: currentUser } = useAuth()
  const { data: users } = useSuspenseQuery(getUsersQueryOptions())

  const tableData: UserTableData[] = users.data.map((user: UserPublic) => ({
    ...user,
    isCurrentUser: currentUser?.id === user.id,
  }))

  return <DataTable columns={columns} data={tableData} />
}

function UsersTable() {
  return (
    <ErrorBoundary
      fallback={
        <Alert variant="destructive">
          <AlertTitle>无法读取用户列表</AlertTitle>
          <AlertDescription>请检查账号权限或稍后重试。</AlertDescription>
        </Alert>
      }
    >
      <Suspense
        fallback={
          <Card className="min-w-0">
            <CardContent className="min-w-0">
              <PendingUsers />
            </CardContent>
          </Card>
        }
      >
        <UsersTableContent />
      </Suspense>
    </ErrorBoundary>
  )
}

function Admin() {
  const { user } = useAuth()
  if (!user) return null
  if (!user.is_superuser)
    return (
      <Alert variant="destructive">
        <AlertTitle>无操作权限</AlertTitle>
        <AlertDescription>
          仅平台管理员可管理用户。请联系管理员获取权限。
        </AlertDescription>
      </Alert>
    )
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1">
          <WorkspacePageTitle>平台管理</WorkspacePageTitle>
          <p className="text-sm text-muted-foreground">
            管理平台用户账号。租户开通与成员分配将在租户管理中提供。
          </p>
        </div>
        <AddUser />
      </div>
      <UsersTable />
    </div>
  )
}
